// The executable of WSolver.app (#479): it starts the viewer that is inside the bundle
// (Resources/python/bin/python3 Resources/viewer/server.py --solver Resources/solver), opens it in
// the browser, and stops it when the app quits. The viewer itself is the web page; this is only
// what a terminal was for: a place the viewer runs in, and a way to stop it.
//
// The viewer runs in a process group of its own, so that quitting takes its solvers with it. It
// gets the usual port when that is free (bookmarks and the page's settings go by the address), any
// free one otherwise; the port is read from the line the viewer prints when it is up. What the
// viewer prints is kept in ~/Library/Logs/WSolver/viewer.log.

#import <Cocoa/Cocoa.h>
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

@interface App : NSObject <NSApplicationDelegate>
@end

@implementation App {
  pid_t _pid;            // the viewer (and its process group); 0 when there is none, or it has ended
  dispatch_source_t _ended;   // fires when the viewer process ends
  BOOL _quitting;
  NSString *_url;        // nil until the viewer is up
  NSString *_logPath;
  NSWindow *_win;
  NSTextField *_status;
  NSButton *_open;
  id _activity;
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
  _win = [[NSWindow alloc] initWithContentRect:NSMakeRect(0, 0, 420, 132)
                                     styleMask:NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable
                                       backing:NSBackingStoreBuffered
                                         defer:NO];
  _win.title = @"WSolver";
  _win.releasedWhenClosed = NO;
  NSView *v = _win.contentView;
  [v addSubview:[self label:@"WSolver" font:[NSFont boldSystemFontOfSize:15] frame:NSMakeRect(20, 92, 380, 20)]];
  _status = [self label:@"Starting…" font:[NSFont systemFontOfSize:13] frame:NSMakeRect(20, 66, 380, 18)];
  _status.selectable = YES;
  _status.textColor = [NSColor secondaryLabelColor];
  [v addSubview:_status];
  NSButton *quit = [NSButton buttonWithTitle:@"Quit" target:NSApp action:@selector(terminate:)];
  quit.frame = NSMakeRect(166, 14, 84, 32);
  [v addSubview:quit];
  _open = [NSButton buttonWithTitle:@"Open in Browser" target:self action:@selector(openInBrowser:)];
  _open.frame = NSMakeRect(256, 14, 150, 32);
  _open.keyEquivalent = @"\r";
  _open.enabled = NO;
  [v addSubview:_open];
  [_win center];
  [_win makeKeyAndOrderFront:nil];
}

- (void)buildMenu {
  NSMenu *bar = [NSMenu new];
  NSMenuItem *appItem = [bar addItemWithTitle:@"" action:nil keyEquivalent:@""];
  NSMenu *app = [NSMenu new];
  [app addItemWithTitle:@"About WSolver" action:@selector(orderFrontStandardAboutPanel:) keyEquivalent:@""];
  [app addItem:[NSMenuItem separatorItem]];
  [app addItemWithTitle:@"Hide WSolver" action:@selector(hide:) keyEquivalent:@"h"];
  [app addItem:[NSMenuItem separatorItem]];
  [app addItemWithTitle:@"Quit WSolver" action:@selector(terminate:) keyEquivalent:@"q"];
  appItem.submenu = app;

  NSMenuItem *fileItem = [bar addItemWithTitle:@"" action:nil keyEquivalent:@""];
  NSMenu *file = [[NSMenu alloc] initWithTitle:@"File"];
  [[file addItemWithTitle:@"Open in Browser" action:@selector(openInBrowser:) keyEquivalent:@"o"] setTarget:self];
  [[file addItemWithTitle:@"Show Log" action:@selector(showLog:) keyEquivalent:@"l"] setTarget:self];
  [file addItem:[NSMenuItem separatorItem]];
  [file addItemWithTitle:@"Close Window" action:@selector(performClose:) keyEquivalent:@"w"];
  fileItem.submenu = file;

  NSMenuItem *editItem = [bar addItemWithTitle:@"" action:nil keyEquivalent:@""];
  NSMenu *edit = [[NSMenu alloc] initWithTitle:@"Edit"];   // the address in the window can be copied
  [edit addItemWithTitle:@"Copy" action:@selector(copy:) keyEquivalent:@"c"];
  [edit addItemWithTitle:@"Select All" action:@selector(selectAll:) keyEquivalent:@"a"];
  editItem.submenu = edit;
  NSApp.mainMenu = bar;
}

- (BOOL)validateMenuItem:(NSMenuItem *)item {
  return item.action == @selector(openInBrowser:) ? _url != nil : YES;
}

- (void)openInBrowser:(id)sender {
  (void)sender;
  if (_url) [[NSWorkspace sharedWorkspace] openURL:[NSURL URLWithString:_url]];
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
  NSString *port = [NSString stringWithFormat:@"%d", portIsFree(kPort) ? kPort : 0];
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
  NSString *dir = [NSHomeDirectory() stringByAppendingPathComponent:@"Library/Logs/WSolver"];
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
  _status.stringValue = [@"Running at " stringByAppendingString:_url];
  _open.enabled = YES;
  if (!getenv("WSOLVER_NO_BROWSER")) [self openInBrowser:nil];   // (for testing the app without a page opening each time)
}

- (void)viewerEnded {
  if (_quitting) return;
  [self failed:_url ? @"The viewer stopped unexpectedly." : @"The viewer did not start."];
}

- (void)failed:(NSString *)what {
  _url = nil;
  _open.enabled = NO;
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
}

/** Opened again (a click in the Dock, a second launch): back to the page if the window was put away. */
- (BOOL)applicationShouldHandleReopen:(NSApplication *)app hasVisibleWindows:(BOOL)visible {
  (void)app;
  [_win makeKeyAndOrderFront:nil];
  if (!visible) [self openInBrowser:nil];
  return NO;
}

- (BOOL)applicationShouldTerminateAfterLastWindowClosed:(NSApplication *)app {
  (void)app;
  return NO;   // the viewer is the page in the browser: the window is only its switch
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
      if ([a runModal] != NSAlertFirstButtonReturn) return NSTerminateCancel;
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
