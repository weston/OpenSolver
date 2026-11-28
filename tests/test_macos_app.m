#define main OpenSolverLauncherMain
#include "../packaging/macos/launcher.m"
#undef main

static NSString *baseURL, *scriptPath, *snapshotPath;

@interface TestApp : App
@end

@implementation TestApp {
  BOOL _reloaded;
}

- (void)applicationDidFinishLaunching:(NSNotification *)note {
  (void)note;
  [self buildMenu];
  [self buildWindow];
  [NSApp activateIgnoringOtherApps:YES];
  dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 90 * NSEC_PER_SEC), dispatch_get_main_queue(), ^{
    fprintf(stderr, "FAIL: native app UI timed out\n");
    exit(1);
  });
  // smoke.py serves an update (OPENSOLVER_UPDATE_URL): it is offered, downloaded and checked first
  if (NSProcessInfo.processInfo.environment[@"OPENSOLVER_UPDATE_URL"].length) [self checkForUpdateSince:1];
  else [self startUI];
}

/** Install Update, as if clicked. */
- (void)offerUpdate:(NSDictionary<NSString *, NSString *> *)offer built:(long long)built {
  (void)built;
  printf("  ok: a newer published installer is offered (%s)\n", offer[@"file"].UTF8String);
  [self downloadUpdate:offer];
}

- (void)updateFailed:(NSString *)why {
  [self failed:[@"update: " stringByAppendingString:why]];
}

/** Not opened: the download is what is checked, then the interface. */
- (void)openInstaller:(NSString *)path {
  NSString *expected = NSProcessInfo.processInfo.environment[@"OPENSOLVER_UPDATE_SHA256"];
  if (![sha256OfFile(path) isEqualToString:expected]) [self failed:@"the downloaded update is not the published one"];
  [[NSFileManager defaultManager] removeItemAtPath:path error:nil];
  printf("  ok: the update is downloaded and its SHA-256 checked\n");
  [self startUI];
}

- (void)startUI {
  WKWebView *web = [self valueForKey:@"web"];
  WKUserScript *errors = [[WKUserScript alloc] initWithSource:
      @"window.testErrors = []; addEventListener('error', e => testErrors.push(e.message));"
       "addEventListener('unhandledrejection', e => testErrors.push(String(e.reason)));"
      injectionTime:WKUserScriptInjectionTimeAtDocumentStart forMainFrameOnly:YES];
  [web.configuration.userContentController addUserScript:errors];
  [self ready:baseURL];
}

- (void)failed:(NSString *)what {
  fprintf(stderr, "FAIL: %s\n", what.UTF8String);
  exit(1);
}

- (void)saveSnapshotAndQuit:(WKWebView *)webView {
  [webView takeSnapshotWithConfiguration:nil completionHandler:^(NSImage *image, NSError *snapshotError) {
    if (snapshotError) [self failed:snapshotError.description];
    NSBitmapImageRep *bitmap = [NSBitmapImageRep imageRepWithData:image.TIFFRepresentation];
    if (![[bitmap representationUsingType:NSBitmapImageFileTypePNG properties:@{}] writeToFile:snapshotPath atomically:YES])
      [self failed:@"could not save the app snapshot"];
    [NSApp terminate:nil];
  }];
}

- (void)testKeyboard:(WKWebView *)webView completionHandler:(void (^)(void))completion {
  NSWindow *window = [self valueForKey:@"win"];
  [webView evaluateJavaScript:@"document.querySelector('[data-view=settings]').click();"
      "document.getElementById('drill-ahead').focus();"
      completionHandler:^(id result, NSError *error) {
    (void)result;
    if (error) [self failed:error.description];
    if (![NSApp sendAction:@selector(selectAll:) to:window.firstResponder from:self])
      [self failed:@"the test window could not perform Select All"];
    NSEvent *key = [NSEvent keyEventWithType:NSEventTypeKeyDown location:NSZeroPoint modifierFlags:0
        timestamp:0 windowNumber:window.windowNumber context:nil characters:@"5" charactersIgnoringModifiers:@"5"
        isARepeat:NO keyCode:23];
    [window sendEvent:key];
    [webView callAsyncJavaScript:@"const end = Date.now() + 2000;"
        "while (document.getElementById('drill-ahead').value !== '5' && Date.now() < end) await new Promise(r => setTimeout(r, 50));"
        "return document.getElementById('drill-ahead').value;"
        arguments:@{} inFrame:nil inContentWorld:WKContentWorld.pageWorld completionHandler:^(id value, NSError *readError) {
      if (readError || ![value isEqual:@"5"]) [self failed:[NSString stringWithFormat:@"native keyboard editing left value %@ (%@)", value, readError]];
      printf("  ok: native Select All and typing edit a setting\n");
      [webView evaluateJavaScript:@"document.querySelector('[data-view=drill]').click()" completionHandler:^(id ignored, NSError *tabError) {
        (void)ignored;
        if (tabError) [self failed:tabError.description];
        completion();
      }];
    }];
  }];
}

- (void)webView:(WKWebView *)webView didFinishNavigation:(WKNavigation *)navigation {
  [super webView:webView didFinishNavigation:navigation];
  NSWindow *window = [self valueForKey:@"win"];
  if (webView.hidden || !(window.styleMask & NSWindowStyleMaskResizable) || window.frame.size.width < 800)
    [self failed:@"the interface is not visible in a resizable app window"];

  NSString *script = _reloaded
      ? @"while (!document.querySelector('#drill-ahead')?.oninput) await new Promise(r => setTimeout(r, 50));"
         "if (document.querySelector('#drill-ahead').value !== '4') throw Error('settings did not survive reload');"
         "if (document.querySelector('#smart-tree').value !== 'strong') throw Error('tree level did not survive reload');"
         "document.querySelector('[data-view=drill]').click();"
         "return 'settings survive reload';"
      : [NSString stringWithContentsOfFile:scriptPath encoding:NSUTF8StringEncoding error:nil];
  [webView callAsyncJavaScript:script arguments:@{} inFrame:nil inContentWorld:WKContentWorld.pageWorld
      completionHandler:^(id result, NSError *error) {
    if (error) [self failed:error.description];
    printf("  ok: %s\n", [[result description] UTF8String]);
    if (!self->_reloaded) {
      self->_reloaded = YES;
      [self reload:nil];
      return;
    }
    [window orderOut:nil];
    [self applicationShouldHandleReopen:NSApp hasVisibleWindows:NO];
    if (!window.visible) [self failed:@"Dock reopen did not restore the window"];
    printf("  ok: Dock reopen restores the app window\n");
    [self testKeyboard:webView completionHandler:^{
      [webView evaluateJavaScript:@"confirm('Confirm test')" completionHandler:^(id accepted, NSError *confirmError) {
        if (confirmError) [self failed:confirmError.description];
        if ([accepted boolValue]) [self failed:@"Cancel confirmed the action"];
        printf("  ok: JavaScript confirmation returns Cancel\n");
        dispatch_async(dispatch_get_main_queue(), ^{
          [self saveSnapshotAndQuit:webView];
        });
      }];
      [NSTimer scheduledTimerWithTimeInterval:0.05 repeats:YES block:^(NSTimer *timer) {
        if (!window.attachedSheet) return;
        [timer invalidate];
        [window endSheet:window.attachedSheet returnCode:NSAlertSecondButtonReturn];
      }];
    }];
  }];
}
@end

/** The update check's decisions (launcher.m, "updates"): which manifests are offered, and the checksum. */
static void testUpdates(void) {
  NSString *hash = @"9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08";   // SHA-256 of "test"
  NSString *text = [NSString stringWithFormat:@"# a comment\nversion 0.1.0\nbuilt 1700000100\n"
                                               "revision abc\nfile OpenSolver-0.1.0-macos-arm64.pkg\nsha256 %@\n", hash];
  NSDictionary *m = parseManifest(text);
  BOOL ok = [m[@"file"] isEqualToString:@"OpenSolver-0.1.0-macos-arm64.pkg"] && [m[@"built"] isEqualToString:@"1700000100"];
  ok &= updateOffer(m, 1700000000) != nil;   // newer: offered
  ok &= updateOffer(m, 1700000100) == nil;   // the same build
  ok &= updateOffer(m, 1800000000) == nil;   // older than this copy
  ok &= updateOffer(m, 0) == nil;            // a copy that does not say when it is from
  NSMutableDictionary *bad = [m mutableCopy];
  bad[@"file"] = @"../../evil.pkg";
  ok &= updateOffer(bad, 1700000000) == nil;
  bad[@"file"] = @"OpenSolver.dmg";
  ok &= updateOffer(bad, 1700000000) == nil;
  bad = [m mutableCopy];
  bad[@"sha256"] = @"1234";
  ok &= updateOffer(bad, 1700000000) == nil;
  bad = [m mutableCopy];
  bad[@"built"] = @"soon";
  ok &= updateOffer(bad, 1700000000) == nil;
  NSString *path = [NSTemporaryDirectory() stringByAppendingPathComponent:@"opensolver-update-test"];
  [@"test" writeToFile:path atomically:YES encoding:NSUTF8StringEncoding error:nil];
  ok &= [sha256OfFile(path) isEqualToString:hash];
  [[NSFileManager defaultManager] removeItemAtPath:path error:nil];
  if (!ok) {
    fprintf(stderr, "FAIL: update manifest checks\n");
    exit(1);
  }
  printf("  ok: the update check offers only a newer, well-formed installer, and checks its SHA-256\n");
}

int main(int argc, const char *argv[]) {
  @autoreleasepool {
    testUpdates();
    if (argc != 4) return 2;
    baseURL = @(argv[1]);
    scriptPath = @(argv[2]);
    snapshotPath = @(argv[3]);
    NSApplication *app = NSApplication.sharedApplication;
    TestApp *delegate = [TestApp new];
    app.delegate = delegate;
    [app setActivationPolicy:NSApplicationActivationPolicyRegular];
    [app run];
  }
  return 0;
}
