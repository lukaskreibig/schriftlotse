#import <Cocoa/Cocoa.h>
#import <WebKit/WebKit.h>
#import <netinet/in.h>
#import <sys/socket.h>
#import <unistd.h>

@interface SchriftLotseDelegate : NSObject <NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler>
@property(nonatomic, strong) NSWindow *window;
@property(nonatomic, strong) WKWebView *webView;
@property(nonatomic, strong) NSTask *backend;
@property(nonatomic, strong) NSURL *localURL;
@property(nonatomic, copy) NSString *instanceToken;
@end

@implementation SchriftLotseDelegate

- (void)applicationDidFinishLaunching:(NSNotification *)notification {
    (void)notification;
    [self installApplicationMenus];
    NSInteger port = [self unusedLoopbackPort];
    self.instanceToken = NSUUID.UUID.UUIDString;
    self.localURL = [NSURL URLWithString:[NSString stringWithFormat:@"http://127.0.0.1:%ld", (long)port]];
    WKWebViewConfiguration *configuration = [[WKWebViewConfiguration alloc] init];
    // SchriftLotse stores state in SQLite, not in browser storage. An ephemeral
    // WebKit store prevents an old stylesheet from surviving an app rebuild.
    configuration.websiteDataStore = [WKWebsiteDataStore nonPersistentDataStore];
    [configuration.userContentController addScriptMessageHandler:self name:@"schriftlotsePicker"];
    NSString *tokenScript = [NSString stringWithFormat:
        @"window.__schriftlotseNativeToken=%@;", [self jsonString:self.instanceToken]];
    [configuration.userContentController addUserScript:[[WKUserScript alloc]
        initWithSource:tokenScript
        injectionTime:WKUserScriptInjectionTimeAtDocumentStart
        forMainFrameOnly:YES]];
    self.webView = [[WKWebView alloc] initWithFrame:NSZeroRect configuration:configuration];
    self.webView.navigationDelegate = self;
    self.webView.UIDelegate = self;
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
    [self startBackendOnPort:port];
}

- (NSString *)jsonString:(NSString *)value {
    NSData *data = [NSJSONSerialization dataWithJSONObject:@[value ?: @""] options:0 error:nil];
    NSString *array = [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding];
    if (array.length < 2) return @"\"\"";
    return [array substringWithRange:NSMakeRange(1, array.length - 2)];
}

- (void)userContentController:(WKUserContentController *)userContentController
      didReceiveScriptMessage:(WKScriptMessage *)message {
    (void)userContentController;
    if (![message.name isEqualToString:@"schriftlotsePicker"] ||
        ![message.body isKindOfClass:NSDictionary.class]) return;
    NSURL *origin = message.frameInfo.request.URL;
    if (!message.frameInfo.mainFrame ||
        !([origin.host isEqualToString:@"127.0.0.1"] || [origin.host isEqualToString:@"localhost"])) return;
    NSString *action = ((NSDictionary *)message.body)[@"action"];
    BOOL folder = [action isEqualToString:@"folder"];
    NSOpenPanel *panel = [NSOpenPanel openPanel];
    panel.canChooseFiles = !folder;
    panel.canChooseDirectories = folder;
    panel.allowsMultipleSelection = !folder;
    panel.resolvesAliases = YES;
    panel.prompt = folder ? @"Archivordner übernehmen" : @"Auswählen";
    [panel beginSheetModalForWindow:self.window completionHandler:^(NSModalResponse result) {
        if (result != NSModalResponseOK) return;
        NSMutableArray<NSString *> *paths = [NSMutableArray array];
        for (NSURL *url in panel.URLs) if (url.path) [paths addObject:url.path];
        NSData *data = [NSJSONSerialization dataWithJSONObject:paths options:0 error:nil];
        NSString *json = [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding] ?: @"[]";
        NSString *script = [NSString stringWithFormat:
            @"window.schriftlotseNativePicked&&window.schriftlotseNativePicked(%@);", json];
        [self.webView evaluateJavaScript:script completionHandler:nil];
    }];
}

- (NSInteger)unusedLoopbackPort {
    int socketFD = socket(AF_INET, SOCK_STREAM, 0);
    if (socketFD < 0) return 7860;
    struct sockaddr_in address = {0};
    address.sin_len = sizeof(address);
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    address.sin_port = 0;
    if (bind(socketFD, (struct sockaddr *)&address, sizeof(address)) != 0) {
        close(socketFD);
        return 7860;
    }
    socklen_t length = sizeof(address);
    getsockname(socketFD, (struct sockaddr *)&address, &length);
    NSInteger port = ntohs(address.sin_port);
    close(socketFD);
    return port > 0 ? port : 7860;
}

- (void)startBackendOnPort:(NSInteger)port {
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
    process.arguments = @[
        @"run", @"--frozen", @"schriftlotse", @"serve", @"--port",
        [NSString stringWithFormat:@"%ld", (long)port]
    ];
    process.currentDirectoryURL = [NSURL fileURLWithPath:root isDirectory:YES];
    NSMutableDictionary<NSString *, NSString *> *environment =
        [NSProcessInfo.processInfo.environment mutableCopy];
    environment[@"SCHRIFTLOTSE_INSTANCE_TOKEN"] = self.instanceToken;
    NSString *currentPath = environment[@"PATH"] ?: @"/usr/bin:/bin";
    environment[@"PATH"] = [NSString stringWithFormat:
        @"/opt/homebrew/bin:/usr/local/bin:/opt/local/bin:%@", currentPath];
    process.environment = environment;
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
    NSURL *healthURL = [self.localURL URLByAppendingPathComponent:@"api/health"];
    NSMutableURLRequest *healthRequest = [NSMutableURLRequest requestWithURL:healthURL];
    [healthRequest setValue:self.instanceToken forHTTPHeaderField:@"x-schriftlotse-instance"];
    NSURLSessionDataTask *task = [[NSURLSession sharedSession]
        dataTaskWithRequest:healthRequest
      completionHandler:^(NSData *data, NSURLResponse *response, NSError *error) {
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

- (void)installApplicationMenus {
    NSMenu *mainMenu = [[NSMenu alloc] initWithTitle:@""];
    NSMenuItem *appItem = [[NSMenuItem alloc] initWithTitle:@"" action:nil keyEquivalent:@""];
    [mainMenu addItem:appItem];
    NSMenu *appMenu = [[NSMenu alloc] initWithTitle:@"SchriftLotse"];
    [appMenu addItemWithTitle:@"SchriftLotse beenden" action:@selector(terminate:) keyEquivalent:@"q"];
    appItem.submenu = appMenu;

    NSMenuItem *editItem = [[NSMenuItem alloc] initWithTitle:@"" action:nil keyEquivalent:@""];
    [mainMenu addItem:editItem];
    NSMenu *editMenu = [[NSMenu alloc] initWithTitle:@"Bearbeiten"];
    [editMenu addItemWithTitle:@"Widerrufen" action:@selector(undo:) keyEquivalent:@"z"];
    [editMenu addItemWithTitle:@"Wiederholen" action:@selector(redo:) keyEquivalent:@"Z"];
    [editMenu addItem:NSMenuItem.separatorItem];
    [editMenu addItemWithTitle:@"Ausschneiden" action:@selector(cut:) keyEquivalent:@"x"];
    [editMenu addItemWithTitle:@"Kopieren" action:@selector(copy:) keyEquivalent:@"c"];
    [editMenu addItemWithTitle:@"Einsetzen" action:@selector(paste:) keyEquivalent:@"v"];
    [editMenu addItemWithTitle:@"Alles auswählen" action:@selector(selectAll:) keyEquivalent:@"a"];
    editItem.submenu = editMenu;
    NSApp.mainMenu = mainMenu;
}

- (void)webView:(WKWebView *)webView
runOpenPanelWithParameters:(WKOpenPanelParameters *)parameters
initiatedByFrame:(WKFrameInfo *)frame
completionHandler:(void (^)(NSArray<NSURL *> * _Nullable URLs))completionHandler {
    (void)webView;
    (void)frame;
    NSOpenPanel *panel = [NSOpenPanel openPanel];
    panel.canChooseFiles = YES;
    panel.canChooseDirectories = NO;
    panel.allowsMultipleSelection = parameters.allowsMultipleSelection;
    panel.resolvesAliases = YES;
    [panel beginSheetModalForWindow:self.window completionHandler:^(NSModalResponse result) {
        completionHandler(result == NSModalResponseOK ? panel.URLs : nil);
    }];
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
    [self.webView.configuration.userContentController removeScriptMessageHandlerForName:@"schriftlotsePicker"];
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
