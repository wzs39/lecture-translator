// Lecture Translator desktop launcher.
// Compiled with the bundled .NET Framework csc.exe (no installs needed):
//   build.bat
// Start: ensures Docker Desktop is running, brings the compose stack up,
// waits for the web service, launches the caption bridge minimized, and
// opens the browser. Stop: stops the stack and the bridge.
// Hidden mode: LectureTranslatorLauncher.exe --selftest  runs the whole
// start sequence headlessly, writes selftest.log, exits 0/1.
using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Threading;
using System.Windows.Forms;

namespace LectureTranslator {
  public class App : Form {
    private readonly Button _startBtn = new Button();
    private readonly Button _stopBtn = new Button();
    private readonly Label _status = new Label();
    private bool _busy;

    private static string AppDir {
      get { return Path.GetDirectoryName(Application.ExecutablePath); }
    }

    // ---- update check ----

    private const string VersionUrl =
      "https://raw.githubusercontent.com/wzs39/lecture-translator/master/version.txt";
    private const string RepoUrl = "https://github.com/wzs39/lecture-translator";

    private static string LocalVersion() {
      try {
        string p = Path.Combine(AppDir, "version.txt");
        return File.Exists(p) ? File.ReadAllText(p).Trim() : "0.0.0";
      } catch {
        return "0.0.0";
      }
    }

    private static string FetchRemoteVersion() {
      try {
        HttpWebRequest req = (HttpWebRequest)WebRequest.Create(VersionUrl);
        req.Timeout = 6000;
        req.UserAgent = "LectureTranslatorLauncher/1.0";
        using (HttpWebResponse resp = (HttpWebResponse)req.GetResponse())
        using (StreamReader r = new StreamReader(resp.GetResponseStream())) {
          return r.ReadToEnd().Trim();
        }
      } catch {
        return null; // offline / GitHub unreachable: skip silently
      }
    }

    private static int[] ParseVersion(string v) {
      int[] r = new int[] { 0, 0, 0 };
      string[] parts = (v ?? "").Split('.');
      for (int i = 0; i < parts.Length && i < 3; i++) {
        int n;
        if (int.TryParse(parts[i], out n) && n >= 0) r[i] = n;
      }
      return r;
    }

    private static int CompareVersions(string a, string b) {
      int[] pa = ParseVersion(a), pb = ParseVersion(b);
      for (int i = 0; i < 3; i++) {
        if (pa[i] != pb[i]) return pa[i] < pb[i] ? -1 : 1;
      }
      return 0;
    }

    private void CheckForUpdate() {
      string remote = FetchRemoteVersion();
      if (string.IsNullOrEmpty(remote)) return;
      if (CompareVersions(remote, LocalVersion()) <= 0) return;
      if (!IsHandleCreated || IsDisposed) return;
      BeginInvoke(new Action(delegate {
        DialogResult r = MessageBox.Show(
          "发现新版本 " + remote + "（当前 " + LocalVersion() + "）。\r\n\r\n" +
          "请到 GitHub 下载最新代码，然后重新双击 install.bat 即可更新。\r\n\r\n" +
          "现在打开 GitHub 页面？",
          "Lecture Translator 有更新", MessageBoxButtons.YesNo,
          MessageBoxIcon.Information);
        if (r == DialogResult.Yes) OpenBrowser();
      }));
    }

    public App() {
      Text = "Lecture Translator 启动器";
      ClientSize = new Size(380, 150);
      StartPosition = FormStartPosition.CenterScreen;
      FormBorderStyle = FormBorderStyle.FixedSingle;
      MaximizeBox = false;

      _status.Text = "未启动";
      _status.Location = new Point(20, 22);
      _status.AutoSize = true;

      _startBtn.Text = "启动";
      _startBtn.Location = new Point(20, 70);
      _startBtn.Size = new Size(150, 40);
      _startBtn.Click += delegate { RunStart(); };

      _stopBtn.Text = "停止";
      _stopBtn.Location = new Point(190, 70);
      _stopBtn.Size = new Size(150, 40);
      _stopBtn.Enabled = false;
      _stopBtn.Click += delegate { RunStop(); };

      Controls.Add(_status);
      Controls.Add(_startBtn);
      Controls.Add(_stopBtn);

      Load += delegate { ThreadPool.QueueUserWorkItem(delegate { CheckForUpdate(); }); };
    }

    private void SetStatus(string text) {
      if (IsHandleCreated && !IsDisposed) {
        BeginInvoke(new Action(delegate { _status.Text = text; }));
      }
    }

    private void SetBusy(bool busy) {
      _busy = busy;
      if (IsHandleCreated && !IsDisposed) {
        BeginInvoke(new Action(delegate {
          _startBtn.Enabled = !busy;
          _stopBtn.Enabled = !busy;
        }));
      }
    }

    private void RunStart() {
      if (_busy) return;
      SetBusy(true);
      ThreadPool.QueueUserWorkItem(delegate {
        try {
          int rc = StartEverything(true);
          SetStatus(rc == 0 ? "已启动：http://localhost:8000" : "启动失败（见说明）");
        } catch (Exception ex) {
          SetStatus("启动出错：" + ex.Message);
        } finally {
          SetBusy(false);
        }
      });
    }

    private void RunStop() {
      if (_busy) return;
      SetBusy(true);
      ThreadPool.QueueUserWorkItem(delegate {
        try {
          StopEverything();
          SetStatus("已停止");
        } catch (Exception ex) {
          SetStatus("停止出错：" + ex.Message);
        } finally {
          SetBusy(false);
        }
      });
    }

    // ---- core logic (also used by --selftest) ----

    private static string FindDocker() {
      string[] candidates = {
        "docker",
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                     "Programs", "DockerDesktop", "resources", "bin", "docker.exe"),
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),
                     "Docker", "Docker", "resources", "bin", "docker.exe")
      };
      foreach (string c in candidates) {
        try {
          Process p = Process.Start(new ProcessStartInfo(c, "info") {
            UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardOutput = true, RedirectStandardError = true,
          });
          p.WaitForExit(8000);
          if (p.HasExited && p.ExitCode == 0) return c;
        } catch { }
      }
      return null;
    }

    private static bool DockerDesktopUp() {
      string d = FindDocker();
      return d != null;
    }

    private static void LaunchDockerDesktop() {
      string[] candidates = {
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                     "Programs", "DockerDesktop", "Docker Desktop.exe"),
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),
                     "Docker", "Docker", "Docker Desktop.exe"),
      };
      bool started = false;
      foreach (string c in candidates) {
        if (File.Exists(c)) {
          Process.Start(c);
          started = true;
          break;
        }
      }
      if (!started) throw new Exception("未找到 Docker Desktop，请手动启动后再运行");
    }

    private static int RunHidden(string file, string args, string workDir, out string output) {
      try {
        Process p = Process.Start(new ProcessStartInfo(file, args) {
          WorkingDirectory = workDir,
          UseShellExecute = false,
          CreateNoWindow = true,
          RedirectStandardOutput = true,
          RedirectStandardError = true,
        });
        output = p.StandardOutput.ReadToEnd() + p.StandardError.ReadToEnd();
        p.WaitForExit(300000);
        return p.HasExited ? p.ExitCode : -1;
      } catch (Exception ex) {
        output = ex.Message;
        return -1;
      }
    }

    private static bool HttpReady() {
      try {
        HttpWebRequest req = (HttpWebRequest)WebRequest.Create("http://localhost:8000/");
        req.Timeout = 5000;
        using (HttpWebResponse resp = (HttpWebResponse)req.GetResponse()) {
          return resp.StatusCode == HttpStatusCode.OK;
        }
      } catch {
        return false;
      }
    }

    private static void LaunchBridge() {
      string venvPython = Path.Combine(AppDir, "bridge", ".venv", "Scripts", "python.exe");
      if (!File.Exists(venvPython)) return; // bridge not set up yet; page still works
      string bat = Path.Combine(AppDir, "start-captions.bat");
      Process.Start(new ProcessStartInfo("cmd.exe",
        "/c start /min \"LectureTranslator-Bridge\" \"" + bat + "\"") {
        CreateNoWindow = true, UseShellExecute = false
      });
    }

    private static bool OpenBrowser() {
      try {
        Process.Start("http://localhost:8000");
        return true;
      } catch {
        return false;
      }
    }

    public static int StartEverything(bool openBrowser) {
      string docker = FindDocker();
      if (docker == null) {
        LaunchDockerDesktop();
        // wait up to 90 s for the daemon
        bool up = false;
        for (int i = 0; i < 30; i++) {
          Thread.Sleep(3000);
          if (DockerDesktopUp()) { up = true; break; }
        }
        if (!up) throw new Exception("Docker Desktop 未在 90 秒内就绪，请手动打开");
        docker = FindDocker();
      }

      string outp;
      int rc = RunHidden(docker, "compose up -d", AppDir, out outp);
      if (rc != 0) throw new Exception("docker compose 失败：" + outp.Trim());

      bool ready = false;
      for (int i = 0; i < 60; i++) {
        Thread.Sleep(2000);
        if (HttpReady()) { ready = true; break; }
      }
      if (!ready) throw new Exception("服务在 2 分钟内未就绪，请查看 docker compose logs");

      LaunchBridge();
      if (openBrowser) OpenBrowser();
      return 0;
    }

    public static void StopEverything() {
      string docker = FindDocker();
      if (docker == null) return;
      string outp;
      RunHidden(docker, "compose stop", AppDir, out outp);
      // stop the caption bridge too (python process running the bridge script)
      RunHidden("powershell.exe",
        "-NoProfile -Command \"Get-CimInstance Win32_Process -Filter \\\"Name='python.exe'\\\" | " +
        "Where-Object {$_.CommandLine -like '*live_captions_bridge*'} | " +
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }\"",
        AppDir, out outp);
    }

    // ---- entry point ----

    [STAThread]
    public static void Main(string[] args) {
      bool selftest = false;
      foreach (string a in args) {
        if (a == "--selftest") { selftest = true; }
      }
      if (selftest) {
        try {
          StartEverything(false);
          File.WriteAllText(Path.Combine(AppDir, "selftest.log"),
                            DateTime.Now.ToString("s") + " OK\n");
          Environment.Exit(0);
        } catch (Exception ex) {
          File.WriteAllText(Path.Combine(AppDir, "selftest.log"),
                            DateTime.Now.ToString("s") + " FAIL: " + ex.Message + "\n");
          Environment.Exit(1);
        }
      }
      Application.EnableVisualStyles();
      Application.Run(new App());
    }
  }
}