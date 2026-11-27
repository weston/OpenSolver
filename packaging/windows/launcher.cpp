// OpenSolver.exe: the Windows app's launcher (#565), the counterpart of packaging/macos/launcher.m.
//
// It starts the viewer (viewer\server.py, with the Python and the solver.exe installed next to it),
// opens it in the default browser, and shows a small window with the address, Open in Browser,
// Show Log and Quit. Closing the window quits, after asking while a solve or the job queue runs.
// Quitting stops the viewer and every solver it started: a Ctrl+C to the viewer's console, as in a
// terminal, and whatever is still there a few seconds later is ended with the job object that
// holds them all. The job object also ends them when this process is killed or crashes, so a
// viewer never outlives its launcher (server.py's --exit-with-parent watches for that on Unix).
//
// The viewer takes port 8765 (or the one it had last time) when that is free and any free port
// otherwise. What it prints goes to %LOCALAPPDATA%\OpenSolver\Logs\viewer.log (the run before is kept
// as viewer.log.1). Its library and job queue are where a viewer run from a checkout keeps them,
// %USERPROFILE%\.wsolver: nothing of the user's is written next to the program.
//
// Opening OpenSolver again while it runs brings this window back and the page up again.
// For packaging/windows/smoke.py: --no-browser, --new-instance (skip the one-instance check),
// --log-dir DIR, and --port N, --library DIR, --jobs DIR, which are passed on to the viewer.
#ifndef UNICODE
#define UNICODE
#endif
#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <commctrl.h>
#include <shellapi.h>
#include <shlobj.h>

#include <atomic>
#include <string>
#include <vector>

namespace {

const wchar_t kClass[] = L"OpenSolverLauncher";
const wchar_t kMutex[] = L"Local\\OpenSolverLauncher";
const wchar_t kRegKey[] = L"Software\\OpenSolver";
const int kPort = 8765;                     // viewer/server.py's default
const char kReady[] = "UI on http://";      // in the line the viewer prints when it is up

enum : UINT { WM_APP_READY = WM_APP + 1, WM_APP_ENDED, WM_APP_SHOW };
enum { ID_OPEN = 100, ID_LOG, ID_QUIT };

struct Options {
  bool browser = true, singleInstance = true;
  std::wstring logDir, port, library, jobs;
};

Options opt;
HWND win, statusLabel, hintLabel, openButton, logButton, quitButton;
HFONT font;
std::wstring url;                 // empty until the viewer is up
std::wstring logPath, appDir;
HANDLE job, viewer;               // the job object; the viewer's process (null once it has ended)
std::atomic<bool> quitting{false};
bool failedOnce, asking;

std::wstring here() {
  std::wstring p(MAX_PATH, L'\0');
  for (;;) {
    DWORD n = GetModuleFileNameW(nullptr, p.data(), (DWORD)p.size());
    if (n < p.size()) {
      p.resize(n);
      break;
    }
    p.resize(p.size() * 2);
  }
  return p.substr(0, p.find_last_of(L"\\/"));
}

std::wstring knownFolder(REFKNOWNFOLDERID id) {
  PWSTR p = nullptr;
  std::wstring out;
  if (SUCCEEDED(SHGetKnownFolderPath(id, KF_FLAG_CREATE, nullptr, &p))) out = p;
  CoTaskMemFree(p);
  return out;
}

/** One argument quoted the way the C runtime (and so Python) splits a command line. */
std::wstring quote(const std::wstring &a) {
  if (!a.empty() && a.find_first_of(L" \t\"") == std::wstring::npos) return a;
  std::wstring out = L"\"";
  size_t slashes = 0;
  for (wchar_t c : a) {
    if (c == L'\\') {
      slashes++;
      continue;
    }
    out.append(c == L'"' ? slashes * 2 + 1 : slashes, L'\\');
    slashes = 0;
    out += c;
  }
  out.append(slashes * 2, L'\\');
  return out + L"\"";
}

std::wstring widen(const std::string &s) {
  int n = MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(), nullptr, 0);
  std::wstring w(n, L'\0');
  MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(), w.data(), n);
  return w;
}

bool portIsFree(int port) {
  SOCKET s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
  if (s == INVALID_SOCKET) return false;
  sockaddr_in a{};
  a.sin_family = AF_INET;
  a.sin_port = htons((u_short)port);
  a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  // No SO_REUSEADDR: on Windows it would let this bind succeed on a port another socket listens on.
  bool ok = bind(s, (sockaddr *)&a, sizeof a) == 0;
  closesocket(s);
  return ok;
}

/** GET a path from the viewer; empty if it does not answer within a second or so. */
std::string get(const std::wstring &path) {
  if (url.empty()) return {};
  int port = _wtoi(url.c_str() + url.find_last_of(L':') + 1);
  SOCKET s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
  if (s == INVALID_SOCKET) return {};
  DWORD ms = 1500;
  setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, (const char *)&ms, sizeof ms);
  setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, (const char *)&ms, sizeof ms);
  sockaddr_in a{};
  a.sin_family = AF_INET;
  a.sin_port = htons((u_short)port);
  a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  std::string out;
  if (connect(s, (sockaddr *)&a, sizeof a) == 0) {
    std::string req = "GET ";
    for (wchar_t c : path) req += (char)c;
    // the port too: the viewer turns away a Host that is not exactly its own address
    req += " HTTP/1.0\r\nHost: 127.0.0.1:" + std::to_string(port) + "\r\nConnection: close\r\n\r\n";
    if (send(s, req.data(), (int)req.size(), 0) == (int)req.size()) {
      char buf[4096];
      int n;
      while ((n = recv(s, buf, sizeof buf, 0)) > 0) out.append(buf, n);
    }
  }
  closesocket(s);
  size_t body = out.find("\r\n\r\n");
  return body == std::string::npos ? std::string() : out.substr(body + 4);
}

bool jsonTrue(const std::string &json, const char *key) {
  std::string k = std::string("\"") + key + "\":";
  std::string compact;
  for (char c : json)
    if (c != ' ') compact += c;
  return compact.find(k + "true") != std::string::npos;
}

std::wstring logTail() {
  HANDLE f = CreateFileW(logPath.c_str(), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                         nullptr, OPEN_EXISTING, 0, nullptr);
  if (f == INVALID_HANDLE_VALUE) return {};
  LARGE_INTEGER size{};
  GetFileSizeEx(f, &size);
  LONGLONG from = size.QuadPart > 600 ? size.QuadPart - 600 : 0;
  LARGE_INTEGER at;
  at.QuadPart = from;
  SetFilePointerEx(f, at, nullptr, FILE_BEGIN);
  std::string s(600, '\0');
  DWORD n = 0;
  ReadFile(f, s.data(), (DWORD)s.size(), &n, nullptr);
  CloseHandle(f);
  s.resize(n);
  return widen(s);
}

void showLog() { ShellExecuteW(win, L"open", logPath.c_str(), nullptr, nullptr, SW_SHOWNORMAL); }

void openBrowser() {
  if (!url.empty()) ShellExecuteW(win, L"open", url.c_str(), nullptr, nullptr, SW_SHOWNORMAL);
}

void setStatus(const std::wstring &status, const std::wstring &hint) {
  SetWindowTextW(statusLabel, status.c_str());
  SetWindowTextW(hintLabel, hint.c_str());
}

// ----- the viewer -------------------------------------------------------------------------------

DWORD WINAPI readViewer(void *pipe) {
  HANDLE in = pipe;
  HANDLE log = CreateFileW(logPath.c_str(), FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                           nullptr, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
  std::string line;
  bool up = false;
  char buf[4096];
  DWORD n;
  while (ReadFile(in, buf, sizeof buf, &n, nullptr) && n > 0) {
    DWORD w;
    if (log != INVALID_HANDLE_VALUE) WriteFile(log, buf, n, &w, nullptr);
    if (up) continue;
    line.append(buf, n);
    size_t at = line.find(kReady);
    size_t nl = at == std::string::npos ? at : line.find('\n', at);
    if (nl == std::string::npos) continue;   // not yet the whole line
    size_t from = at + 6;                    // "UI on " is 6 characters: the address starts after it
    std::string address = line.substr(from, nl - from);
    while (!address.empty() && (address.back() == '\r' || address.back() == ' ')) address.pop_back();
    up = true;
    PostMessageW(win, WM_APP_READY, 0, (LPARAM) new std::wstring(widen(address)));
  }
  if (log != INVALID_HANDLE_VALUE) CloseHandle(log);
  CloseHandle(in);
  return 0;
}

// The viewer's end is its process ending, not the end of what is read above: its solvers write to
// the same pipe, and one that outlived it would keep that open.
DWORD WINAPI waitViewer(void *process) {
  WaitForSingleObject(process, INFINITE);
  PostMessageW(win, WM_APP_ENDED, 0, 0);
  return 0;
}

int preferredPort() {
  DWORD port = 0, size = sizeof port;
  if (RegGetValueW(HKEY_CURRENT_USER, kRegKey, L"ViewerPort", RRF_RT_REG_DWORD, nullptr, &port, &size) != ERROR_SUCCESS ||
      port == 0 || port > 65535)
    port = kPort;
  return (int)port;
}

bool startViewer() {
  std::wstring dir = opt.logDir;
  if (dir.empty()) dir = knownFolder(FOLDERID_LocalAppData) + L"\\OpenSolver\\Logs";
  SHCreateDirectoryExW(nullptr, dir.c_str(), nullptr);
  logPath = dir + L"\\viewer.log";
  MoveFileExW(logPath.c_str(), (logPath + L".1").c_str(), MOVEFILE_REPLACE_EXISTING);   // the run before this one is kept

  std::wstring port = opt.port;
  if (port.empty()) {
    int preferred = preferredPort();
    port = std::to_wstring(portIsFree(preferred) ? preferred : 0);
  }
  // -u: the lines as they are printed; -B: nothing written into the install folder; -E -s: this
  // Python and its own library only, whatever the environment says
  std::wstring cmd = quote(appDir + L"\\python\\python.exe") + L" -u -B -E -s " +
                     quote(appDir + L"\\viewer\\server.py") + L" --solver " + quote(appDir + L"\\solver.exe") +
                     L" --port " + quote(port);
  if (!opt.library.empty()) cmd += L" --library " + quote(opt.library);
  if (!opt.jobs.empty()) cmd += L" --jobs " + quote(opt.jobs);

  SECURITY_ATTRIBUTES inherit{sizeof inherit, nullptr, TRUE};
  HANDLE readEnd, writeEnd;
  if (!CreatePipe(&readEnd, &writeEnd, &inherit, 0)) return false;
  SetHandleInformation(readEnd, HANDLE_FLAG_INHERIT, 0);
  HANDLE nul = CreateFileW(L"NUL", GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, &inherit, OPEN_EXISTING, 0, nullptr);

  // Only these handles reach the viewer (and, through it, its solvers).
  HANDLE handles[] = {nul, writeEnd};
  SIZE_T size = 0;
  InitializeProcThreadAttributeList(nullptr, 1, 0, &size);
  std::vector<char> attrs(size);
  auto list = (LPPROC_THREAD_ATTRIBUTE_LIST)attrs.data();
  InitializeProcThreadAttributeList(list, 1, 0, &size);
  UpdateProcThreadAttribute(list, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST, handles, sizeof handles, nullptr, nullptr);
  STARTUPINFOEXW si{};
  si.StartupInfo.cb = sizeof si;
  si.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
  si.StartupInfo.hStdInput = nul;
  si.StartupInfo.hStdOutput = writeEnd;
  si.StartupInfo.hStdError = writeEnd;
  si.lpAttributeList = list;
  PROCESS_INFORMATION pi{};
  std::wstring home = knownFolder(FOLDERID_Profile);   // relative paths the viewer is given are the user's
  // A console of its own that is never shown (its solvers share it, so they show none either),
  // started suspended so that it is in the job before it can start anything.
  BOOL ok = CreateProcessW(nullptr, cmd.data(), nullptr, nullptr, TRUE,
                           CREATE_NO_WINDOW | CREATE_SUSPENDED | EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT,
                           nullptr, home.empty() ? nullptr : home.c_str(), &si.StartupInfo, &pi);
  DeleteProcThreadAttributeList(list);
  CloseHandle(writeEnd);
  CloseHandle(nul);
  if (!ok) {
    CloseHandle(readEnd);
    return false;
  }
  if (!AssignProcessToJobObject(job, pi.hProcess)) {
    TerminateProcess(pi.hProcess, 1);
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    CloseHandle(readEnd);
    return false;
  }
  ResumeThread(pi.hThread);
  CloseHandle(pi.hThread);
  viewer = pi.hProcess;
  CloseHandle(CreateThread(nullptr, 0, readViewer, readEnd, 0, nullptr));
  HANDLE waited;
  DuplicateHandle(GetCurrentProcess(), viewer, GetCurrentProcess(), &waited, SYNCHRONIZE, FALSE, 0);
  CloseHandle(CreateThread(nullptr, 0, waitViewer, waited, 0, nullptr));
  return true;
}

/** Ctrl+C to the viewer's console, as in a terminal: the viewer closes down and its solvers, which
 *  share that console, go with it. Whatever has not gone after a few seconds is ended. */
void stopViewer() {
  if (viewer) {
    if (WaitForSingleObject(viewer, 0) == WAIT_TIMEOUT && AttachConsole(GetProcessId(viewer))) {
      SetConsoleCtrlHandler(nullptr, TRUE);   // not this process: it is attached to that console now
      GenerateConsoleCtrlEvent(CTRL_C_EVENT, 0);
      FreeConsole();
    }
    WaitForSingleObject(viewer, 5000);
  }
  TerminateJobObject(job, 1);
}

void failed(const std::wstring &what) {
  if (failedOnce || quitting) return;
  failedOnce = true;
  url.clear();
  setStatus(what, L"");
  std::wstring tail = logTail();
  std::wstring text = what + L"\n\n" + (tail.empty() ? L"It printed nothing." : L"The end of its log:\n\n" + tail) +
                      L"\n\nOpen the log?";
  if (MessageBoxW(win, text.c_str(), L"OpenSolver", MB_YESNO | MB_ICONERROR) == IDYES) showLog();
  quitting = true;
  stopViewer();
  DestroyWindow(win);
}

/** What Quit and closing the window do: ask first while something is running, then stop the
 *  viewer and end. False when the user said not to. */
bool quit(bool ask) {
  if (quitting) return true;
  if (ask && !url.empty() && viewer) {
    bool solving = jsonTrue(get(L"/api/status"), "solving");
    bool jobs = !solving && jsonTrue(get(L"/api/jobs/state"), "running");
    if (solving || jobs) {
      asking = true;
      int answer = MessageBoxW(win,
                               solving ? L"A solve is running.\n\nQuitting stops it, and a tree that is not saved is lost."
                                       : L"The job queue is running.\n\nQuitting stops the job that is running.",
                               L"Quit OpenSolver?", MB_OKCANCEL | MB_ICONWARNING | MB_DEFBUTTON2);
      asking = false;
      if (answer != IDOK) {
        if (!viewer) PostMessageW(win, WM_APP_ENDED, 0, 0);   // it ended meanwhile
        return false;
      }
    }
  }
  quitting = true;
  setStatus(L"Stopping OpenSolver…", L"");
  UpdateWindow(win);
  stopViewer();
  DestroyWindow(win);
  return true;
}

// ----- the window -------------------------------------------------------------------------------

int dpi() { return win ? (int)GetDpiForWindow(win) : 96; }
int px(int v) { return MulDiv(v, dpi(), 96); }

void layout() {
  NONCLIENTMETRICSW m{};
  m.cbSize = sizeof m;
  SystemParametersInfoForDpi(SPI_GETNONCLIENTMETRICS, sizeof m, &m, 0, dpi());
  HFONT f = CreateFontIndirectW(&m.lfMessageFont);
  for (HWND h : {statusLabel, hintLabel, openButton, logButton, quitButton}) SendMessageW(h, WM_SETFONT, (WPARAM)f, TRUE);
  if (font) DeleteObject(font);
  font = f;
  RECT r;
  GetClientRect(win, &r);
  int w = r.right - r.left, pad = px(16), bh = px(28), bw = px(120), gap = px(8);
  MoveWindow(statusLabel, pad, pad, w - 2 * pad, px(20), TRUE);
  MoveWindow(hintLabel, pad, pad + px(26), w - 2 * pad, px(40), TRUE);
  int y = r.bottom - pad - bh;
  MoveWindow(quitButton, w - pad - bw, y, bw, bh, TRUE);
  MoveWindow(logButton, w - pad - 2 * bw - gap, y, bw, bh, TRUE);
  MoveWindow(openButton, w - pad - 3 * bw - 2 * gap, y, bw, bh, TRUE);
}

LRESULT CALLBACK wndProc(HWND h, UINT msg, WPARAM wp, LPARAM lp) {
  switch (msg) {
    case WM_CREATE: {
      win = h;
      auto make = [&](const wchar_t *cls, const wchar_t *text, DWORD style, int id) {
        return CreateWindowExW(0, cls, text, WS_CHILD | WS_VISIBLE | style, 0, 0, 0, 0, h, (HMENU)(INT_PTR)id, nullptr, nullptr);
      };
      statusLabel = make(L"STATIC", L"Starting OpenSolver…", SS_LEFT | SS_ENDELLIPSIS, 0);
      hintLabel = make(L"STATIC", L"", SS_LEFT, 0);
      openButton = make(L"BUTTON", L"Open in Browser", BS_DEFPUSHBUTTON | WS_TABSTOP | WS_DISABLED, ID_OPEN);
      logButton = make(L"BUTTON", L"Show Log", BS_PUSHBUTTON | WS_TABSTOP, ID_LOG);
      quitButton = make(L"BUTTON", L"Quit", BS_PUSHBUTTON | WS_TABSTOP, ID_QUIT);
      layout();
      return 0;
    }
    case WM_SIZE:
      layout();
      return 0;
    case WM_DPICHANGED: {
      RECT *r = (RECT *)lp;
      SetWindowPos(h, nullptr, r->left, r->top, r->right - r->left, r->bottom - r->top, SWP_NOZORDER | SWP_NOACTIVATE);
      layout();
      return 0;
    }
    case WM_CTLCOLORSTATIC:
      SetBkMode((HDC)wp, TRANSPARENT);
      if ((HWND)lp == hintLabel) SetTextColor((HDC)wp, GetSysColor(COLOR_GRAYTEXT));
      return (LRESULT)GetSysColorBrush(COLOR_WINDOW);
    case WM_COMMAND:
      switch (LOWORD(wp)) {
        case ID_OPEN: openBrowser(); return 0;
        case ID_LOG: showLog(); return 0;
        case ID_QUIT: quit(true); return 0;
      }
      break;
    case WM_CLOSE:
      quit(true);
      return 0;
    case WM_QUERYENDSESSION:
      return TRUE;
    case WM_ENDSESSION:
      if (wp) {   // Windows is signing out or shutting down: no asking
        quitting = true;
        stopViewer();
      }
      return 0;
    case WM_APP_READY: {
      auto *address = (std::wstring *)lp;
      if (!quitting && !failedOnce) {
        url = *address;
        if (opt.port.empty()) {
          DWORD port = (DWORD)_wtoi(url.c_str() + url.find_last_of(L':') + 1);
          RegSetKeyValueW(HKEY_CURRENT_USER, kRegKey, L"ViewerPort", REG_DWORD, &port, sizeof port);
        }
        setStatus(L"OpenSolver is running at " + url,
                  L"It opens in your browser. Closing this window stops OpenSolver and any solve it is running.");
        EnableWindow(openButton, TRUE);
        if (opt.browser) openBrowser();
      }
      delete address;
      return 0;
    }
    case WM_APP_ENDED:
      if (viewer) {
        CloseHandle(viewer);
        viewer = nullptr;
      }
      TerminateJobObject(job, 1);   // whatever is left of it has nothing to run for
      if (quitting || asking) return 0;   // asking: said when that question is answered, not on top of it
      Sleep(250);                        // a moment for its last words to reach the log, which is quoted
      failed(url.empty() ? L"The viewer did not start." : L"The viewer stopped unexpectedly.");
      return 0;
    case WM_APP_SHOW:   // opened again
      if (IsIconic(h)) ShowWindow(h, SW_RESTORE);
      SetForegroundWindow(h);
      if (opt.browser) openBrowser();
      return 0;
    case WM_DESTROY:
      PostQuitMessage(0);
      return 0;
  }
  return DefWindowProcW(h, msg, wp, lp);
}

void parseArgs() {
  int argc = 0;
  wchar_t **argv = CommandLineToArgvW(GetCommandLineW(), &argc);
  for (int i = 1; i < argc; i++) {
    std::wstring a = argv[i];
    auto next = [&]() { return i + 1 < argc ? std::wstring(argv[++i]) : std::wstring(); };
    if (a == L"--no-browser") opt.browser = false;
    else if (a == L"--new-instance") opt.singleInstance = false;
    else if (a == L"--log-dir") opt.logDir = next();
    else if (a == L"--port") opt.port = next();
    else if (a == L"--library") opt.library = next();
    else if (a == L"--jobs") opt.jobs = next();
  }
  LocalFree(argv);
}

}  // namespace

int WINAPI wWinMain(HINSTANCE inst, HINSTANCE, PWSTR, int show) {
  parseArgs();
  if (opt.singleInstance) {
    CreateMutexW(nullptr, FALSE, kMutex);   // held until this process ends
    if (GetLastError() == ERROR_ALREADY_EXISTS) {
      for (int i = 0; i < 20; i++) {   // the first one may still be making its window
        if (HWND other = FindWindowW(kClass, nullptr)) {
          DWORD pid = 0;
          GetWindowThreadProcessId(other, &pid);
          AllowSetForegroundWindow(pid);
          PostMessageW(other, WM_APP_SHOW, 0, 0);
          break;
        }
        Sleep(100);
      }
      return 0;
    }
  }
#ifdef OPENSOLVER_ISA
  // The solver is built for this instruction set (Makefile: WIN_ARCH); on an older CPU it would
  // crash at its first instruction of it. Say so instead.
  __builtin_cpu_init();
  if (!__builtin_cpu_supports(OPENSOLVER_ISA)) {
    MessageBoxW(nullptr,
                L"This computer's processor is too old for this build of OpenSolver: it needs "
                L"AVX2 (Intel Haswell, AMD Excavator or Ryzen, and later).",
                L"OpenSolver", MB_OK | MB_ICONERROR);
    return 1;
  }
#endif

  // A Ctrl+C is how the viewer is stopped: it must not be ignored by inheritance (a process
  // started in a new process group ignores it, and passes that on).
  SetConsoleCtrlHandler(nullptr, FALSE);
  INITCOMMONCONTROLSEX icc{sizeof icc, ICC_STANDARD_CLASSES};
  InitCommonControlsEx(&icc);
  WSADATA wsa;
  WSAStartup(MAKEWORD(2, 2), &wsa);
  appDir = here();

  // Everything the viewer starts is in this job, and the job ends with this process.
  job = CreateJobObjectW(nullptr, nullptr);
  JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits{};
  limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
  SetInformationJobObject(job, JobObjectExtendedLimitInformation, &limits, sizeof limits);

  WNDCLASSEXW wc{};
  wc.cbSize = sizeof wc;
  wc.lpfnWndProc = wndProc;
  wc.hInstance = inst;
  wc.hIcon = LoadIconW(inst, MAKEINTRESOURCEW(1));
  wc.hIconSm = (HICON)LoadImageW(inst, MAKEINTRESOURCEW(1), IMAGE_ICON, GetSystemMetrics(SM_CXSMICON),
                                 GetSystemMetrics(SM_CYSMICON), 0);
  wc.hCursor = LoadCursorW(nullptr, (LPCWSTR)IDC_ARROW);
  wc.hbrBackground = GetSysColorBrush(COLOR_WINDOW);
  wc.lpszClassName = kClass;
  RegisterClassExW(&wc);

  DWORD style = WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX;
  UINT sysDpi = GetDpiForSystem();
  RECT r{0, 0, MulDiv(460, sysDpi, 96), MulDiv(140, sysDpi, 96)};
  AdjustWindowRectExForDpi(&r, style, FALSE, 0, sysDpi);
  HWND h = CreateWindowExW(0, kClass, L"OpenSolver", style, CW_USEDEFAULT, CW_USEDEFAULT, r.right - r.left,
                           r.bottom - r.top, nullptr, nullptr, inst, nullptr);
  if (!h) return 1;
  // sized for the system DPI; on a monitor with another one, size it again for that
  if (GetDpiForWindow(h) != sysDpi) {
    RECT c{0, 0, px(460), px(140)};
    AdjustWindowRectExForDpi(&c, style, FALSE, 0, dpi());
    SetWindowPos(h, nullptr, 0, 0, c.right - c.left, c.bottom - c.top, SWP_NOMOVE | SWP_NOZORDER);
  }
  ShowWindow(h, show);
  UpdateWindow(h);

  if (!startViewer()) failed(L"The viewer inside OpenSolver could not be started.");

  MSG m;
  while (GetMessageW(&m, nullptr, 0, 0) > 0) {
    if (!IsDialogMessageW(h, &m)) {
      TranslateMessage(&m);
      DispatchMessageW(&m);
    }
  }
  if (!quitting) stopViewer();
  return 0;
}
