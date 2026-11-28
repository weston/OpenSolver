#import <Cocoa/Cocoa.h>
#import <CommonCrypto/CommonDigest.h>
#import <WebKit/WebKit.h>
#include <arpa/inet.h>
#include <netinet/in.h>
#include <signal.h>
#include <spawn.h>
#include <sys/socket.h>
#include <sys/wait.h>

extern char **environ;

static const int kPort = 8765;                 // viewer/server.py's default
static NSString *const kReady = @"UI on http://";   // in the line the viewer prints when it is up

static BOOL portIsFree(int port) {
  int fd = socket(AF_INET, SOCK_STREAM, 0);
  if (fd < 0) return NO;
  int one = 1;
  setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);   // as the viewer's own bind does
  struct sockaddr_in a = {0};
  a.sin_family = AF_INET;
  a.sin_port = htons(port);
  a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  BOOL ok = bind(fd, (struct sockaddr *)&a, sizeof a) == 0;
  close(fd);
  return ok;
}

/** GET a JSON object from the viewer; nil if it does not answer within the second. */
static NSDictionary *getJSON(NSString *url) {
  NSMutableURLRequest *req = [NSMutableURLRequest requestWithURL:[NSURL URLWithString:url]];
  req.timeoutInterval = 1.0;
  __block NSDictionary *out = nil;
  dispatch_semaphore_t done = dispatch_semaphore_create(0);
  [[[NSURLSession sharedSession] dataTaskWithRequest:req
                                   completionHandler:^(NSData *data, NSURLResponse *resp, NSError *err) {
    (void)resp;
    if (data && !err) {
      id v = [NSJSONSerialization JSONObjectWithData:data options:0 error:nil];
      if ([v isKindOfClass:[NSDictionary class]]) out = v;
    }
    dispatch_semaphore_signal(done);
  }] resume];
  dispatch_semaphore_wait(done, dispatch_time(DISPATCH_TIME_NOW, (int64_t)(1.5 * NSEC_PER_SEC)));
  return out;
}

// ----- updates ----------------------------------------------------------------------------------
// At launch the app reads installers/macos/latest.txt from the project's main branch on GitHub, a
// few "key value" lines that packaging/macos/build.sh writes next to the installer:
//   version 0.1.0
//   built 1764280000        the commit time of the source it was built from (Unix seconds)
//   revision <git commit>
//   file OpenSolver-0.1.0-macos-arm64.pkg
//   sha256 <the installer's SHA-256>
// When "built" is later than this app's own (OpenSolverBuildTime in Info.plist), it asks whether to
// install it: the installer is downloaded next to it, checked against its SHA-256, opened, and the
// app quits so that it can be replaced. Without a network, or with no newer build, nothing is said.
// OPENSOLVER_UPDATE_URL replaces the folder the two are read from (tests); OPENSOLVER_NO_UPDATE_CHECK
// turns the check off.
static NSString *const kUpdateFolder = @"https://raw.githubusercontent.com/weston/OpenSolver/main/installers/macos/";

/** The "key value" lines of an update manifest. */
static NSDictionary<NSString *, NSString *> *parseManifest(NSString *text) {
  NSMutableDictionary<NSString *, NSString *> *out = [NSMutableDictionary dictionary];
  for (NSString *raw in [text componentsSeparatedByCharactersInSet:NSCharacterSet.newlineCharacterSet]) {
    NSString *line = [raw stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceCharacterSet];
    NSRange gap = [line rangeOfCharacterFromSet:NSCharacterSet.whitespaceCharacterSet];
    if (!line.length || [line hasPrefix:@"#"] || gap.location == NSNotFound) continue;
    out[[line substringToIndex:gap.location]] =
        [[line substringFromIndex:gap.location] stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceCharacterSet];
  }
  return out;
}

static BOOL matches(NSString *s, NSString *pattern) {
  return s && [s rangeOfString:pattern options:NSRegularExpressionSearch].location != NSNotFound;
}

/** The manifest when it names an installer built later than `built` (well formed: a plain .pkg name and
 *  a 64-digit SHA-256), else nil. */
static NSDictionary<NSString *, NSString *> *updateOffer(NSDictionary<NSString *, NSString *> *m, long long built) {
  if (built <= 0 || !matches(m[@"built"], @"^[0-9]{1,12}$") || m[@"built"].longLongValue <= built) return nil;
  if (!matches(m[@"file"], @"^[A-Za-z0-9][A-Za-z0-9._+-]*\\.pkg$")) return nil;
  if (!matches(m[@"sha256"].lowercaseString, @"^[0-9a-f]{64}$")) return nil;
  return m;
}

/** The SHA-256 of a file, in lowercase hex; nil if it cannot be read. */
static NSString *sha256OfFile(NSString *path) {
  NSData *data = [NSData dataWithContentsOfFile:path options:NSDataReadingMappedIfSafe error:nil];
  if (!data) return nil;
  unsigned char digest[CC_SHA256_DIGEST_LENGTH];
  CC_SHA256(data.bytes, (CC_LONG)data.length, digest);
  NSMutableString *hex = [NSMutableString stringWithCapacity:2 * CC_SHA256_DIGEST_LENGTH];
  for (int i = 0; i < CC_SHA256_DIGEST_LENGTH; i++) [hex appendFormat:@"%02x", digest[i]];
  return hex;
}

static NSString *updateFolder(void) {
  NSString *env = NSProcessInfo.processInfo.environment[@"OPENSOLVER_UPDATE_URL"];
  if (!env.length) return kUpdateFolder;
  return [env hasSuffix:@"/"] ? env : [env stringByAppendingString:@"/"];
}

static NSString *builtDate(long long seconds) {
  return [NSDateFormatter localizedStringFromDate:[NSDate dateWithTimeIntervalSince1970:(NSTimeInterval)seconds]
                                        dateStyle:NSDateFormatterMediumStyle
                                        timeStyle:NSDateFormatterShortStyle];
}

@interface App : NSObject <NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate>
@end

@implementation App {
  pid_t _pid;            // the viewer (and its process group); 0 when there is none, or it has ended
  dispatch_source_t _ended;   // fires when the viewer process ends
  BOOL _quitting;
  BOOL _asking;          // the "quit while it is running?" alert is up
  BOOL _failed;          // the failure has been said (once is enough)
  NSString *_url;        // nil until the viewer is up
  NSString *_logPath;
  NSWindow *_win;
  WKWebView *_web;
  NSTextField *_status;
  NSProgressIndicator *_spinner;
  id _activity;
  NSURLSessionDownloadTask *_download;   // the update being downloaded, if one is
}

// ----- the window and the menu ------------------------------------------------------------------

- (NSTextField *)label:(NSString *)text font:(NSFont *)font frame:(NSRect)frame {
  NSTextField *t = [[NSTextField alloc] initWithFrame:frame];
  t.stringValue = text;
  t.font = font;
  t.editable = NO;
  t.bordered = NO;
  t.drawsBackground = NO;
  t.lineBreakMode = NSLineBreakByTruncatingMiddle;
  return t;
}

- (void)buildWindow {
  NSRect screen = [NSScreen mainScreen].visibleFrame;
  NSRect frame = NSMakeRect(0, 0, MIN(1280, screen.size.width), MIN(860, screen.size.height - 40));
  _win = [[NSWindow alloc] initWithContentRect:frame
                                     styleMask:NSWindowStyleMaskTitled | NSWindowStyleMaskClosable |
                                               NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable
                                       backing:NSBackingStoreBuffered
                                         defer:NO];
  _win.title = @"OpenSolver";
  _win.releasedWhenClosed = NO;
  _win.minSize = NSMakeSize(800, 600);
  [_win center];
  [_win setFrameAutosaveName:@"OpenSolverWindow"];
  NSView *v = _win.contentView;
  _web = [[WKWebView alloc] initWithFrame:v.bounds configuration:[WKWebViewConfiguration new]];
  _web.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
  _web.navigationDelegate = self;
  _web.UIDelegate = self;
  _web.hidden = YES;
  [v addSubview:_web];
  CGFloat x = NSMidX(v.bounds), y = NSMidY(v.bounds);
  _spinner = [[NSProgressIndicator alloc] initWithFrame:NSMakeRect(x - 16, y, 32, 32)];
  _spinner.style = NSProgressIndicatorStyleSpinning;
  _spinner.autoresizingMask = NSViewMinXMargin | NSViewMaxXMargin | NSViewMinYMargin | NSViewMaxYMargin;
  [_spinner startAnimation:nil];
  [v addSubview:_spinner];
  _status = [self label:@"Starting…" font:[NSFont systemFontOfSize:13] frame:NSMakeRect(x - 190, y - 30, 380, 20)];
  _status.autoresizingMask = _spinner.autoresizingMask;
  _status.alignment = NSTextAlignmentCenter;
  _status.textColor = [NSColor secondaryLabelColor];
  [v addSubview:_status];
  [_win makeKeyAndOrderFront:nil];
}

- (void)buildMenu {
  NSMenu *bar = [NSMenu new];
  NSMenuItem *appItem = [bar addItemWithTitle:@"" action:nil keyEquivalent:@""];
  NSMenu *app = [NSMenu new];
  [app addItemWithTitle:@"About OpenSolver" action:@selector(orderFrontStandardAboutPanel:) keyEquivalent:@""];
  [app addItem:[NSMenuItem separatorItem]];
  [app addItemWithTitle:@"Hide OpenSolver" action:@selector(hide:) keyEquivalent:@"h"];
  [app addItem:[NSMenuItem separatorItem]];
  [app addItemWithTitle:@"Quit OpenSolver" action:@selector(terminate:) keyEquivalent:@"q"];
  appItem.submenu = app;

  NSMenuItem *fileItem = [bar addItemWithTitle:@"" action:nil keyEquivalent:@""];
  NSMenu *file = [[NSMenu alloc] initWithTitle:@"File"];
  [[file addItemWithTitle:@"Show Log" action:@selector(showLog:) keyEquivalent:@"l"] setTarget:self];
  [file addItem:[NSMenuItem separatorItem]];
  [file addItemWithTitle:@"Close Window" action:@selector(performClose:) keyEquivalent:@"w"];
  fileItem.submenu = file;

  NSMenuItem *editItem = [bar addItemWithTitle:@"" action:nil keyEquivalent:@""];
  NSMenu *edit = [[NSMenu alloc] initWithTitle:@"Edit"];
  [edit addItemWithTitle:@"Undo" action:@selector(undo:) keyEquivalent:@"z"];
  [edit addItemWithTitle:@"Redo" action:@selector(redo:) keyEquivalent:@"Z"];
  [edit addItem:[NSMenuItem separatorItem]];
  [edit addItemWithTitle:@"Cut" action:@selector(cut:) keyEquivalent:@"x"];
  [edit addItemWithTitle:@"Copy" action:@selector(copy:) keyEquivalent:@"c"];
  [edit addItemWithTitle:@"Paste" action:@selector(paste:) keyEquivalent:@"v"];
  [edit addItemWithTitle:@"Select All" action:@selector(selectAll:) keyEquivalent:@"a"];
  editItem.submenu = edit;

  NSMenuItem *viewItem = [bar addItemWithTitle:@"" action:nil keyEquivalent:@""];
  NSMenu *view = [[NSMenu alloc] initWithTitle:@"View"];
  [[view addItemWithTitle:@"Reload" action:@selector(reload:) keyEquivalent:@"r"] setTarget:self];
  [view addItemWithTitle:@"Enter Full Screen" action:@selector(toggleFullScreen:) keyEquivalent:@"f"]
      .keyEquivalentModifierMask = NSEventModifierFlagControl | NSEventModifierFlagCommand;
  viewItem.submenu = view;

  NSMenuItem *windowItem = [bar addItemWithTitle:@"" action:nil keyEquivalent:@""];
  NSMenu *window = [[NSMenu alloc] initWithTitle:@"Window"];
  [window addItemWithTitle:@"Minimize" action:@selector(performMiniaturize:) keyEquivalent:@"m"];
  [window addItemWithTitle:@"Zoom" action:@selector(performZoom:) keyEquivalent:@""];
  windowItem.submenu = window;
  NSApp.windowsMenu = window;
  NSApp.mainMenu = bar;
}

- (BOOL)validateMenuItem:(NSMenuItem *)item {
  return item.action == @selector(reload:) ? _url != nil : YES;
}

- (void)reload:(id)sender {
  (void)sender;
  [_web reload];
}

- (void)showLog:(id)sender {
  (void)sender;
  [[NSWorkspace sharedWorkspace] openURL:[NSURL fileURLWithPath:_logPath]];
}

// ----- the viewer -------------------------------------------------------------------------------

/** Start the viewer with its output on a pipe; -1 if it cannot be started. */
- (int)spawnViewer {
  NSString *res = [NSBundle mainBundle].resourcePath;
  NSString *python = [res stringByAppendingPathComponent:@"python/bin/python3"];
  int preferredPort = (int)[[NSUserDefaults standardUserDefaults] integerForKey:@"ViewerPort"];
  if (!preferredPort) preferredPort = kPort;
  NSString *port = [NSString stringWithFormat:@"%d", portIsFree(preferredPort) ? preferredPort : 0];
  // -u: the lines as they are printed; -B: nothing written into the bundle; -E -s: this Python and
  // its own library only, whatever the environment says
  NSArray<NSString *> *args = @[python, @"-u", @"-B", @"-E", @"-s",
                                [res stringByAppendingPathComponent:@"viewer/server.py"],
                                @"--solver", [res stringByAppendingPathComponent:@"solver"],
                                @"--port", port,
                                @"--exit-with-parent", [NSString stringWithFormat:@"%d", getpid()]];
  int fds[2];
  if (pipe(fds) != 0) return -1;
  char **argv = calloc(args.count + 1, sizeof(char *));
  for (NSUInteger i = 0; i < args.count; i++) argv[i] = strdup(args[i].fileSystemRepresentation);
  posix_spawn_file_actions_t fa;
  posix_spawn_file_actions_init(&fa);
  posix_spawn_file_actions_addopen(&fa, 0, "/dev/null", O_RDONLY, 0);
  posix_spawn_file_actions_adddup2(&fa, fds[1], 1);
  posix_spawn_file_actions_adddup2(&fa, fds[1], 2);
  posix_spawn_file_actions_addclose(&fa, fds[0]);
  posix_spawn_file_actions_addclose(&fa, fds[1]);
  posix_spawnattr_t at;
  posix_spawnattr_init(&at);
  posix_spawnattr_setpgroup(&at, 0);                       // a group of its own: see -stopViewer
  // No signal ignored or blocked because it was for whatever started the app (a shell that runs
  // it in the background has it ignore Ctrl-C): the viewer is stopped with one.
  sigset_t all, none;
  sigfillset(&all);
  sigemptyset(&none);
  posix_spawnattr_setsigdefault(&at, &all);
  posix_spawnattr_setsigmask(&at, &none);
  posix_spawnattr_setflags(&at, POSIX_SPAWN_SETPGROUP | POSIX_SPAWN_CLOEXEC_DEFAULT |
                                POSIX_SPAWN_SETSIGDEF | POSIX_SPAWN_SETSIGMASK);
  pid_t pid = 0;
  int rc = posix_spawn(&pid, argv[0], &fa, &at, argv, environ);
  posix_spawn_file_actions_destroy(&fa);
  posix_spawnattr_destroy(&at);
  for (NSUInteger i = 0; i < args.count; i++) free(argv[i]);
  free(argv);
  close(fds[1]);
  if (rc != 0) {
    close(fds[0]);
    return -1;
  }
  _pid = pid;
  return fds[0];
}

- (void)startViewer {
  NSString *dir = [NSHomeDirectory() stringByAppendingPathComponent:@"Library/Logs/OpenSolver"];
  [[NSFileManager defaultManager] createDirectoryAtPath:dir withIntermediateDirectories:YES attributes:nil error:nil];
  _logPath = [dir stringByAppendingPathComponent:@"viewer.log"];
  NSString *old = [_logPath stringByAppendingString:@".1"];   // the run before this one is kept
  [[NSFileManager defaultManager] removeItemAtPath:old error:nil];
  [[NSFileManager defaultManager] moveItemAtPath:_logPath toPath:old error:nil];
  [[NSFileManager defaultManager] createFileAtPath:_logPath contents:nil attributes:nil];
  NSFileHandle *log = [NSFileHandle fileHandleForWritingAtPath:_logPath];

  int fd = [self spawnViewer];
  if (fd < 0) {
    [self failed:@"The viewer inside the app could not be started."];
    return;
  }
  pid_t pid = _pid;
  dispatch_async(dispatch_get_global_queue(QOS_CLASS_UTILITY, 0), ^{
    NSMutableData *line = [NSMutableData data];
    BOOL up = NO;
    char buf[4096];
    ssize_t n;
    while ((n = read(fd, buf, sizeof buf)) > 0) {
      [log writeData:[NSData dataWithBytes:buf length:(NSUInteger)n]];
      if (up) continue;
      [line appendBytes:buf length:(NSUInteger)n];
      NSString *text = [[NSString alloc] initWithData:line encoding:NSUTF8StringEncoding];
      NSRange at = text ? [text rangeOfString:kReady] : NSMakeRange(NSNotFound, 0);
      NSRange nl = at.location == NSNotFound ? at
          : [text rangeOfString:@"\n" options:0 range:NSMakeRange(at.location, text.length - at.location)];
      if (nl.location == NSNotFound) continue;   // not yet the whole line
      NSUInteger from = at.location + 6;          // "UI on " is 6 characters: the address starts after it
      NSString *url = [text substringWithRange:NSMakeRange(from, nl.location - from)];
      up = YES;
      dispatch_async(dispatch_get_main_queue(), ^{ [self ready:url]; });
    }
    close(fd);
  });

  // The viewer's end is its process ending, not the end of what is read above: its solvers write
  // their errors to the same pipe, and one that outlived it would keep that open.
  _ended = dispatch_source_create(DISPATCH_SOURCE_TYPE_PROC, (uintptr_t)pid, DISPATCH_PROC_EXIT, dispatch_get_main_queue());
  dispatch_source_set_event_handler(_ended, ^{
    dispatch_source_cancel(self->_ended);
    // Whatever is left of its group has nothing to run for. Sent before the viewer is reaped:
    // until then its pid, which names the group, cannot have become another process's.
    killpg(pid, SIGKILL);
    int st = 0;
    while (waitpid(pid, &st, 0) < 0 && errno == EINTR) {}
    self->_pid = 0;
    if (self->_quitting) return;
    // a moment for its last words to reach the log, which the alert quotes
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, NSEC_PER_SEC / 4), dispatch_get_main_queue(), ^{ [self viewerEnded]; });
  });
  dispatch_resume(_ended);
}

- (void)ready:(NSString *)url {
  _url = [url stringByTrimmingCharactersInSet:[NSCharacterSet whitespaceAndNewlineCharacterSet]];
  [[NSUserDefaults standardUserDefaults] setInteger:[NSURL URLWithString:_url].port.integerValue forKey:@"ViewerPort"];
  [_web loadRequest:[NSURLRequest requestWithURL:[NSURL URLWithString:_url]]];
}

- (void)webView:(WKWebView *)webView didFinishNavigation:(WKNavigation *)navigation {
  (void)navigation;
  [_spinner stopAnimation:nil];
  _spinner.hidden = YES;
  _status.hidden = YES;
  webView.hidden = NO;
  [_win makeFirstResponder:webView];
}

- (void)webView:(WKWebView *)webView decidePolicyForNavigationAction:(WKNavigationAction *)action
    decisionHandler:(void (^)(WKNavigationActionPolicy))decisionHandler {
  NSURL *url = action.request.URL;
  NSURL *local = [NSURL URLWithString:_url];
  BOOL sameOrigin = [url.scheme isEqualToString:local.scheme] && [url.host isEqualToString:local.host] &&
                    [url.port isEqual:local.port];
  if (sameOrigin && action.targetFrame) {
    decisionHandler(WKNavigationActionPolicyAllow);
    return;
  }
  decisionHandler(WKNavigationActionPolicyCancel);
  if (sameOrigin) [webView loadRequest:action.request];
  else if (action.navigationType == WKNavigationTypeLinkActivated &&
           ([@"https" isEqualToString:url.scheme] || [@"http" isEqualToString:url.scheme]))
    [[NSWorkspace sharedWorkspace] openURL:url];
}

- (void)webView:(WKWebView *)webView didFailProvisionalNavigation:(WKNavigation *)navigation withError:(NSError *)error {
  (void)webView;
  (void)navigation;
  if (error.code != NSURLErrorCancelled) [self failed:error.localizedDescription];
}

- (void)webView:(WKWebView *)webView didFailNavigation:(WKNavigation *)navigation withError:(NSError *)error {
  [self webView:webView didFailProvisionalNavigation:navigation withError:error];
}

- (void)webView:(WKWebView *)webView runJavaScriptConfirmPanelWithMessage:(NSString *)message
    initiatedByFrame:(WKFrameInfo *)frame completionHandler:(void (^)(BOOL))completionHandler {
  (void)webView;
  (void)frame;
  NSAlert *alert = [NSAlert new];
  alert.messageText = message;
  [alert addButtonWithTitle:@"OK"];
  [alert addButtonWithTitle:@"Cancel"];
  [alert beginSheetModalForWindow:_win completionHandler:^(NSModalResponse response) {
    completionHandler(response == NSAlertFirstButtonReturn);
  }];
}

- (void)viewerEnded {
  if (_quitting || _asking || _failed) return;   // asking: said when that alert is answered, not on top of it
  [self failed:_url ? @"The viewer stopped unexpectedly." : @"The viewer did not start."];
}

- (void)failed:(NSString *)what {
  _failed = YES;
  _url = nil;
  _status.stringValue = what;
  NSString *tail = @"";
  NSString *all = [NSString stringWithContentsOfFile:_logPath encoding:NSUTF8StringEncoding error:nil];
  if (all.length) tail = all.length > 600 ? [all substringFromIndex:all.length - 600] : all;
  NSAlert *a = [NSAlert new];
  a.alertStyle = NSAlertStyleCritical;
  a.messageText = what;
  a.informativeText = tail.length ? [@"The end of its log:\n\n" stringByAppendingString:tail] : @"It printed nothing.";
  [a addButtonWithTitle:@"Quit"];
  [a addButtonWithTitle:@"Show Log"];
  if ([a runModal] == NSAlertSecondButtonReturn) [self showLog:nil];
  _quitting = YES;
  [NSApp terminate:nil];
}

/** Ctrl-C to the viewer's whole group, as in a terminal: the viewer closes down, its solvers go
 *  with it. Whatever has not gone after a few seconds is killed. */
- (void)stopViewer {
  pid_t pid = _pid;
  if (pid <= 0) return;
  killpg(pid, SIGINT);
  for (int i = 0; i < 50 && _pid; i++)   // _pid is cleared on the main queue when the viewer has ended
    [[NSRunLoop currentRunLoop] runUntilDate:[NSDate dateWithTimeIntervalSinceNow:0.1]];
  if (_pid) killpg(pid, SIGKILL);   // still there (so the pid is still its own): no more asking
}

// ----- updates (see kUpdateFolder) ---------------------------------------------------------------

- (void)checkForUpdate {
  if (NSProcessInfo.processInfo.environment[@"OPENSOLVER_NO_UPDATE_CHECK"].length) return;
  [self checkForUpdateSince:[[NSBundle mainBundle].infoDictionary[@"OpenSolverBuildTime"] longLongValue]];
}

- (void)checkForUpdateSince:(long long)built {
  if (built <= 0) return;   // a build that does not say when its source is from cannot tell what is newer
  NSURL *url = [NSURL URLWithString:[updateFolder() stringByAppendingString:@"latest.txt"]];
  if (!url) return;
  NSMutableURLRequest *req = [NSMutableURLRequest requestWithURL:url];
  req.cachePolicy = NSURLRequestReloadIgnoringLocalCacheData;
  req.timeoutInterval = 20;
  [[[NSURLSession sharedSession] dataTaskWithRequest:req
                                   completionHandler:^(NSData *data, NSURLResponse *resp, NSError *err) {
    NSInteger status = [resp isKindOfClass:[NSHTTPURLResponse class]] ? ((NSHTTPURLResponse *)resp).statusCode : 200;
    if (err || !data || status != 200) return;   // offline, or nothing published: say nothing
    NSString *text = [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding];
    NSDictionary *offer = text ? updateOffer(parseManifest(text), built) : nil;
    if (offer) dispatch_async(dispatch_get_main_queue(), ^{ [self offerUpdate:offer built:built]; });
  }] resume];
}

- (void)offerUpdate:(NSDictionary<NSString *, NSString *> *)offer built:(long long)built {
  if (_quitting || _failed || _asking || _win.attachedSheet) return;
  NSAlert *a = [NSAlert new];
  a.messageText = @"A new version of OpenSolver is available.";
  a.informativeText = [NSString stringWithFormat:
      @"This copy is from %@; the new one is from %@.\n\nInstalling it downloads the installer, opens it, "
       "and quits OpenSolver so that it can be replaced.", builtDate(built), builtDate(offer[@"built"].longLongValue)];
  [a addButtonWithTitle:@"Install Update"];
  [a addButtonWithTitle:@"Not Now"];
  [a beginSheetModalForWindow:_win completionHandler:^(NSModalResponse answer) {
    if (answer == NSAlertFirstButtonReturn) [self downloadUpdate:offer];
  }];
}

- (void)updateFailed:(NSString *)why {
  NSAlert *a = [NSAlert new];
  a.alertStyle = NSAlertStyleWarning;
  a.messageText = @"The update could not be installed.";
  a.informativeText = [why stringByAppendingString:@"\n\nOpenSolver will offer it again the next time it opens."];
  [a addButtonWithTitle:@"OK"];
  [a beginSheetModalForWindow:_win completionHandler:nil];
}

/** The downloaded, checked installer: opened, and the app quits so that it can be replaced. */
- (void)openInstaller:(NSString *)path {
  if (![[NSWorkspace sharedWorkspace] openURL:[NSURL fileURLWithPath:path]]) {
    [self updateFailed:@"The installer could not be opened."];
    return;
  }
  [NSApp terminate:nil];   // asks first when a solve or the job queue runs
}

- (void)downloadUpdate:(NSDictionary<NSString *, NSString *> *)offer {
  NSString *name = offer[@"file"], *expected = offer[@"sha256"].lowercaseString;
  NSURL *url = [NSURL URLWithString:[updateFolder() stringByAppendingString:name]];
  NSString *dir = [NSTemporaryDirectory() stringByAppendingPathComponent:@"OpenSolver-update"];
  NSString *dest = [dir stringByAppendingPathComponent:name];
  NSAlert *sheet = [NSAlert new];
  sheet.messageText = @"Downloading the update…";
  sheet.informativeText = name;
  NSProgressIndicator *bar = [[NSProgressIndicator alloc] initWithFrame:NSMakeRect(0, 0, 300, 20)];
  bar.indeterminate = NO;
  bar.minValue = 0;
  bar.maxValue = 1;
  sheet.accessoryView = bar;
  [sheet addButtonWithTitle:@"Cancel"];
  NSURLSessionDownloadTask *task = [[NSURLSession sharedSession] downloadTaskWithURL:url
      completionHandler:^(NSURL *location, NSURLResponse *resp, NSError *err) {
    // the downloaded file is removed when this returns: it is moved first, here
    NSInteger status = [resp isKindOfClass:[NSHTTPURLResponse class]] ? ((NSHTTPURLResponse *)resp).statusCode : 200;
    NSString *why = nil;
    if (err) why = err.code == NSURLErrorCancelled ? @"" : err.localizedDescription;
    else if (status != 200) why = [NSString stringWithFormat:@"The download failed (HTTP %ld).", (long)status];
    else {
      NSFileManager *fm = [NSFileManager defaultManager];
      [fm createDirectoryAtPath:dir withIntermediateDirectories:YES attributes:nil error:nil];
      [fm removeItemAtPath:dest error:nil];
      NSError *moveError = nil;
      if (![fm moveItemAtURL:location toURL:[NSURL fileURLWithPath:dest] error:&moveError])
        why = moveError.localizedDescription;
      else if (![sha256OfFile(dest) isEqualToString:expected]) {
        [fm removeItemAtPath:dest error:nil];
        why = @"The downloaded installer is not the one that was published (its SHA-256 differs).";
      }
    }
    dispatch_async(dispatch_get_main_queue(), ^{
      self->_download = nil;
      if (self->_win.attachedSheet) [self->_win endSheet:self->_win.attachedSheet returnCode:NSModalResponseStop];
      if (why) {
        if (why.length) [self updateFailed:why];
        return;
      }
      [self openInstaller:dest];
    });
  }];
  _download = task;
  [sheet beginSheetModalForWindow:_win completionHandler:^(NSModalResponse answer) {
    if (answer == NSAlertFirstButtonReturn) [task cancel];   // Cancel (a finished download ends it with Stop)
  }];
  NSTimer *timer = [NSTimer timerWithTimeInterval:0.2 repeats:YES block:^(NSTimer *t) {
    if (self->_download != task) {
      [t invalidate];
      return;
    }
    bar.doubleValue = task.progress.fractionCompleted;
  }];
  [[NSRunLoop mainRunLoop] addTimer:timer forMode:NSRunLoopCommonModes];
  [task resume];
}

// ----- the application --------------------------------------------------------------------------

- (void)applicationDidFinishLaunching:(NSNotification *)note {
  (void)note;
  [self buildMenu];
  [self buildWindow];
  [NSApp activateIgnoringOtherApps:YES];
  // The app's work is done by what it starts, for hours and with its window put away: it is not
  // to be napped or ended by the system as an app that does nothing. (The Mac may still sleep.)
  _activity = [[NSProcessInfo processInfo] beginActivityWithOptions:NSActivityUserInitiatedAllowingIdleSystemSleep
                                                             reason:@"the viewer and its solvers run inside this app"];
  [self startViewer];
  [self checkForUpdate];
}

/** Opened again (a click in the Dock, a second launch): back to the page if the window was put away. */
- (BOOL)applicationShouldHandleReopen:(NSApplication *)app hasVisibleWindows:(BOOL)visible {
  (void)app;
  (void)visible;
  [_win deminiaturize:nil];
  [_win makeKeyAndOrderFront:nil];
  return NO;
}

- (BOOL)applicationShouldTerminateAfterLastWindowClosed:(NSApplication *)app {
  (void)app;
  return NO;
}

- (NSApplicationTerminateReply)applicationShouldTerminate:(NSApplication *)app {
  (void)app;
  if (_quitting || !_pid) return NSTerminateNow;
  if (_url) {
    BOOL solving = [getJSON([_url stringByAppendingString:@"/api/status"])[@"solving"] boolValue];
    BOOL jobs = [getJSON([_url stringByAppendingString:@"/api/jobs/state"])[@"running"] boolValue];
    if (solving || jobs) {
      NSAlert *a = [NSAlert new];
      a.messageText = solving ? @"A solve is running." : @"The job queue is running.";
      a.informativeText = solving ? @"Quitting stops it, and a tree that is not saved is lost."
                                  : @"Quitting stops the job that is running.";
      [a addButtonWithTitle:@"Quit"];
      [a addButtonWithTitle:@"Cancel"];
      _asking = YES;
      NSModalResponse answer = [a runModal];
      _asking = NO;
      if (answer != NSAlertFirstButtonReturn) {
        if (!_pid) dispatch_async(dispatch_get_main_queue(), ^{ [self viewerEnded]; });   // it ended meanwhile
        return NSTerminateCancel;
      }
    }
  }
  _quitting = YES;
  [self stopViewer];
  return NSTerminateNow;
}

@end

int main(void) {
  @autoreleasepool {
    NSApplication *app = [NSApplication sharedApplication];
    App *delegate = [App new];
    app.delegate = delegate;
    [app setActivationPolicy:NSApplicationActivationPolicyRegular];
    [app run];
  }
  return 0;
}
