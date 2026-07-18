#import <Cocoa/Cocoa.h>
#import <WebKit/WebKit.h>

@interface SchriftLotseDelegate : NSObject <NSApplicationDelegate, WKNavigationDelegate>
@property(nonatomic, strong) NSWindow *window;
@property(nonatomic, strong) WKWebView *webView;
@property(nonatomic, strong) NSTask *backend;
@property(nonatomic, strong) NSURL *localURL;
@end

@implementation SchriftLotseDelegate

- (void)applicationDidFinishLaunching:(NSNotification *)notification {
    (void)notification;
    self.localURL = [NSURL URLWithString:@"http://127.0.0.1:7860"];
    WKWebViewConfiguration *configuration = [[WKWebViewConfiguration alloc] init];
    // SchriftLotse stores state in SQLite, not in browser storage. An ephemeral
    // WebKit store prevents an old stylesheet from surviving an app rebuild.
    configuration.websiteDataStore = [WKWebsiteDataStore nonPersistentDataStore];
    self.webView = [[WKWebView alloc] initWithFrame:NSZeroRect configuration:configuration];
    self.webView.navigationDelegate = self;
    self.webView.allowsMagnification = NO;

    NSRect frame = NSMakeRect(0, 0, 1320, 860);
    NSWindowStyleMask style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable |
        NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable |
        NSWindowStyleMaskFullSizeContentView;
    self.window = [[NSWindow alloc] initWithContentRect:frame
                                              styleMask:style
                                                backing:NSBackingStoreBuffered
                                                  defer:NO];
    self.window.title = @"SchriftLotse";
    // Below this size the three working areas would have to become a long
    // scrolling web page. Keep the native app in its compact dashboard layout.
    self.window.minSize = NSMakeSize(1100, 800);
    self.window.contentView = self.webView;
    [self.window center];
    [self.window makeKeyAndOrderFront:nil];
    [NSApp activateIgnoringOtherApps:YES];
    [self showMessage:@"SchriftLotse startet lokal …"
                detail:@"Modelle und Dokumente bleiben auf diesem Mac."];
    [self checkServerThenStart];
}

- (void)checkServerThenStart {
    __weak typeof(self) weakSelf = self;
    NSURLSessionDataTask *task = [[NSURLSession sharedSession]
        dataTaskWithURL:self.localURL
      completionHandler:^(NSData *data, NSURLResponse *response, NSError *error) {
        (void)data;
        (void)error;
        SchriftLotseDelegate *selfRef = weakSelf;
        if (!selfRef) return;
        NSInteger status = [(NSHTTPURLResponse *)response statusCode];
        dispatch_async(dispatch_get_main_queue(), ^{
            if (status == 200) {
                [selfRef.webView loadRequest:[NSURLRequest requestWithURL:selfRef.localURL]];
            } else {
                [selfRef startBackend];
            }
        });
    }];
    [task resume];
}

- (void)startBackend {
    NSURL *resources = NSBundle.mainBundle.resourceURL;
    NSURL *rootFile = [resources URLByAppendingPathComponent:@"repository.txt"];
    NSError *readError = nil;
    NSString *root = [NSString stringWithContentsOfURL:rootFile
                                              encoding:NSUTF8StringEncoding
                                                 error:&readError];
    if (!root) {
        [self showMessage:@"Repository nicht gefunden"
                    detail:@"Bitte die App mit scripts/build_macos_app.sh neu erstellen."];
        return;
    }
    root = [root stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceAndNewlineCharacterSet];
    NSArray<NSString *> *candidates = @[
        @"/opt/homebrew/bin/uv", @"/usr/local/bin/uv", @"~/.local/bin/uv", @"~/.cargo/bin/uv"
    ];
    NSString *uv = nil;
    for (NSString *candidate in candidates) {
        NSString *expanded = candidate.stringByExpandingTildeInPath;
        if ([NSFileManager.defaultManager isExecutableFileAtPath:expanded]) {
            uv = expanded;
            break;
        }
    }
    if (!uv) {
        [self showMessage:@"uv fehlt"
                    detail:@"Bitte zuerst SchriftLotse.command starten oder uv über Homebrew installieren."];
        return;
    }

    NSTask *process = [[NSTask alloc] init];
    process.executableURL = [NSURL fileURLWithPath:uv];
    process.arguments = @[@"run", @"--frozen", @"schriftlotse", @"serve", @"--port", @"7860"];
    process.currentDirectoryURL = [NSURL fileURLWithPath:root isDirectory:YES];
    NSString *logDirectory = @"~/Library/Logs".stringByExpandingTildeInPath;
    NSString *logPath = [logDirectory stringByAppendingPathComponent:@"SchriftLotse.log"];
    [NSFileManager.defaultManager createDirectoryAtPath:logDirectory
                            withIntermediateDirectories:YES attributes:nil error:nil];
    if (![NSFileManager.defaultManager fileExistsAtPath:logPath]) {
        [NSFileManager.defaultManager createFileAtPath:logPath contents:nil attributes:nil];
    }
    NSFileHandle *log = [NSFileHandle fileHandleForWritingAtPath:logPath];
    [log seekToEndOfFile];
    process.standardOutput = log;
    process.standardError = log;
    __weak typeof(self) weakSelf = self;
    process.terminationHandler = ^(NSTask *finished) {
        if (finished.terminationStatus == 0) return;
        dispatch_async(dispatch_get_main_queue(), ^{
            [weakSelf showMessage:@"Lokaler Dienst wurde beendet"
                           detail:@"Starte SchriftLotse.command einmal zur Einrichtung und öffne die App erneut."];
        });
    };
    NSError *launchError = nil;
    if (![process launchAndReturnError:&launchError]) {
        [self showMessage:@"SchriftLotse konnte nicht starten"
                    detail:launchError.localizedDescription];
        return;
    }
    self.backend = process;
    [self pollServer:0];
}

- (void)pollServer:(NSInteger)attempt {
    if (attempt >= 240) {
        [self showMessage:@"Start dauert ungewöhnlich lange"
                    detail:@"Bitte SchriftLotse.command öffnen und das Terminalprotokoll prüfen."];
        return;
    }
    __weak typeof(self) weakSelf = self;
    NSURLSessionDataTask *task = [[NSURLSession sharedSession]
        dataTaskWithURL:self.localURL
      completionHandler:^(NSData *data, NSURLResponse *response, NSError *error) {
        (void)data;
        (void)error;
        SchriftLotseDelegate *selfRef = weakSelf;
        if (!selfRef) return;
        NSInteger status = [(NSHTTPURLResponse *)response statusCode];
        dispatch_async(dispatch_get_main_queue(), ^{
            if (status == 200) {
                [selfRef.webView loadRequest:[NSURLRequest requestWithURL:selfRef.localURL]];
            } else {
                dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(0.5 * NSEC_PER_SEC)),
                               dispatch_get_main_queue(), ^{ [selfRef pollServer:attempt + 1]; });
            }
        });
    }];
    [task resume];
}

- (void)showMessage:(NSString *)title detail:(NSString *)detail {
    NSString *html = [NSString stringWithFormat:
        @"<!doctype html><meta charset='utf-8'><style>"
         "body{margin:0;display:grid;place-items:center;height:100vh;background:#f4f2ec;color:#173f4b;font:16px -apple-system}"
         "div{text-align:center}h1{font:30px Georgia;margin:0 0 8px}p{color:#68777d;max-width:520px}"
         "</style><div><h1>%@</h1><p>%@</p></div>", title, detail];
    [self.webView loadHTMLString:html baseURL:nil];
}

- (void)applicationWillTerminate:(NSNotification *)notification {
    (void)notification;
    if (self.backend.running) [self.backend terminate];
}

- (BOOL)applicationShouldTerminateAfterLastWindowClosed:(NSApplication *)sender {
    (void)sender;
    return YES;
}

@end

int main(int argc, const char *argv[]) {
    (void)argc;
    (void)argv;
    @autoreleasepool {
        NSApplication *application = NSApplication.sharedApplication;
        SchriftLotseDelegate *delegate = [[SchriftLotseDelegate alloc] init];
        application.delegate = delegate;
        [application setActivationPolicy:NSApplicationActivationPolicyRegular];
        [application run];
    }
    return 0;
}
