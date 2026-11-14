// The setup wizard's custom actions. They run in the wizard (the
// InstallUISequence), as the user, and leave the work to ubo-lpa.py: the MSI
// carries Python, ubo-lpa.py and the bundled uBO release as one zip in its
// Binary table, which UboProbe unpacks into the temporary folder.

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Text;
using System.Web.Script.Serialization;
using WixToolset.Dtf.WindowsInstaller;

namespace UboSetup
{
    public static class CustomActions
    {
        // The Chrome page has a checkbox for each of this many profiles.
        const int MaxProfiles = 8;

        // Unpack the payload, ask ubo-lpa.py setup-info about Chrome, its
        // profiles and the uBO releases, and set the properties the wizard
        // shows. A failure leaves the wizard usable: it then shows the error
        // and the defaults.
        [CustomAction]
        public static ActionResult UboProbe(Session session)
        {
            try
            {
                string payload = Unpack(session);
                session["UBOLPA_PAYLOAD"] = payload;
                var args = new List<string> {
                    "setup-info", "--dir", Folder(session["UBOLPA_DIR"]), "--ubo-zip", BundledZip(payload),
                };
                string output = RunPython(session, payload, args, out int code);
                if (code != 0)
                    throw new InvalidOperationException($"ubo-lpa.py setup-info exited with {code}: {Tail(output)}");
                // setup-info prints one JSON line, after anything Python wrote to stderr.
                string json = output.Split('\n').Last(line => line.TrimStart().StartsWith("{"));
                Apply(session, (Dictionary<string, object>)new JavaScriptSerializer().DeserializeObject(json));
            }
            catch (Exception e)
            {
                session.Log("UboProbe: " + e);
                session["UBOLPA_PROBE_ERROR"] = Escape(e.Message);
            }
            return ActionResult.Success;
        }

        static void Apply(Session session, Dictionary<string, object> info)
        {
            var chrome = (Dictionary<string, object>)info["chrome"];
            if (Equals(chrome["found"], true))
            {
                string version = Str(chrome, "version") ?? "(version not found)";
                string scope = Str(chrome, "scope") == "user" ? "installed for this user" : "installed for all users";
                session["UBOLPA_CHROME_FOUND"] = "1";
                session["UBOLPA_CHROME_TEXT"] = Escape($"Google Chrome {version}, {scope}");
            }
            session["UBOLPA_USER_DATA"] = Escape(Str(chrome, "user_data") ?? "");

            var profiles = ((object[])info["profiles"]).Cast<Dictionary<string, object>>().ToList();
            int shown = Math.Min(profiles.Count, MaxProfiles);
            for (int i = 0; i < shown; i++)
            {
                var profile = profiles[i];
                string label = Str(profile, "name");
                string account = Str(profile, "account");
                if (!string.IsNullOrEmpty(account))
                    label += $" ({account})";
                label += $", folder {Str(profile, "dir")}";
                bool loaded = Equals(profile["loaded"], true);
                if (loaded)
                    label += ", already has uBlock Origin";
                string n = (i + 1).ToString();
                session[$"UBOLPA_P{n}_DIR"] = Str(profile, "dir");
                session[$"UBOLPA_P{n}_LABEL"] = Escape(label);
                session[$"UBOLPA_P{n}"] = "1";
                if (loaded)
                    session[$"UBOLPA_P{n}_LOADED"] = "1";
            }
            session["UBOLPA_PROFILE_COUNT"] = shown.ToString();
            if (profiles.Count > MaxProfiles)
                session["UBOLPA_PROFILES_MORE"] = (profiles.Count - MaxProfiles).ToString();

            var ubo = (Dictionary<string, object>)info["ubo"];
            string latest = Str(ubo, "latest");
            if (latest != null)
            {
                session["UBOLPA_LATEST"] = latest;
                session["UBOLPA_LATEST_URL"] = Str(ubo, "latest_url");
            }
            else
            {
                // GitHub could not be reached, so only the bundled release is sure to install.
                session["UBOLPA_SOURCE"] = "bundled";
            }
            string bundled = Str(ubo, "bundled");
            if (bundled != null)
            {
                session["UBOLPA_BUNDLED"] = bundled;
                session["UBOLPA_BUNDLED_URL"] = Str(ubo, "bundled_url");
            }

            var install = (Dictionary<string, object>)info["install"];
            string installed = Str(install, "version");
            if (installed != null)
                session["UBOLPA_INSTALLED_VERSION"] = installed;
            // In maintenance the checkbox starts as the scheduled task is now.
            if (session.EvaluateCondition("Installed"))
                session["AUTOUPDATE"] = Equals(install["autoupdate"], true) ? "1" : "";
        }

        // The Chrome page's Next button: the ticked profiles, as load --profiles
        // takes them. Without Chrome, or with every profile unticked, Setup does
        // not load. A Chrome that has never run has no profile yet; load then
        // starts it, and it creates its first one.
        [CustomAction]
        public static ActionResult UboSelectProfiles(Session session)
        {
            int.TryParse(session["UBOLPA_PROFILE_COUNT"], out int count);
            var chosen = new List<string>();
            for (int i = 1; i <= count; i++)
                if (!string.IsNullOrEmpty(session[$"UBOLPA_P{i}"]))
                    chosen.Add(session[$"UBOLPA_P{i}_DIR"]);
            session["UBOLPA_PROFILES"] = string.Join("|", chosen);
            bool load = string.IsNullOrEmpty(session["UBOLPA_PROBE_ERROR"])
                ? !string.IsNullOrEmpty(session["UBOLPA_CHROME_FOUND"]) && (count == 0 || chosen.Count > 0)
                : true;
            session["UBOLPA_SKIP_LOAD"] = load ? "" : "1";
            return ActionResult.Success;
        }

        // Install without maintenance: build uBO in the chosen folder and load
        // it into Chrome, from the unpacked payload, while the progress page is
        // shown. Nothing else is installed or registered.
        [CustomAction]
        public static ActionResult UboPortable(Session session)
        {
            string payload = session["UBOLPA_PAYLOAD"];
            if (string.IsNullOrEmpty(payload) || !Directory.Exists(payload))
            {
                Error(session, "Setup could not unpack its files: " + session["UBOLPA_PROBE_ERROR"]);
                return ActionResult.Failure;
            }
            string folder = Folder(session["UBOLPA_DIR"]);

            Progress(session, "Building uBlock Origin and its toolbar button");
            var install = new List<string> { "install", "--non-interactive", "--dir", folder };
            if (session["UBOLPA_SOURCE"] == "bundled")
                install.AddRange(new[] { "--ubo-zip", BundledZip(payload) });
            string output = RunPython(session, payload, install, out int code);
            if (code != 0)
            {
                Error(session, "Setup could not build uBlock Origin:\n\n" + Tail(output));
                return ActionResult.Failure;
            }

            if (string.IsNullOrEmpty(session["UBOLPA_SKIP_LOAD"]))
            {
                Progress(session, "Loading into Chrome: click Allow in Chrome's prompt");
                var load = new List<string> { "load", "--dir", folder };
                if (!string.IsNullOrEmpty(session["UBOLPA_PROFILES"]))
                    load.AddRange(new[] { "--profiles", session["UBOLPA_PROFILES"] });
                // A refused prompt does not fail Setup: load then opens the
                // steps for loading by hand, as in a maintained install.
                RunPython(session, payload, load, out _);
            }
            return ActionResult.Success;
        }

        [CustomAction]
        public static ActionResult UboCleanup(Session session)
        {
            string payload = session["UBOLPA_PAYLOAD"];
            if (!string.IsNullOrEmpty(payload))
                TryDelete(payload);
            return ActionResult.Success;
        }

        static string Unpack(Session session)
        {
            string temp = Path.GetTempPath();
            // Earlier runs that were cancelled leave their copy behind.
            foreach (string old in Directory.GetDirectories(temp, "ubo-lpa-setup-*"))
                TryDelete(old);
            string dir = Path.Combine(temp, "ubo-lpa-setup-" + Guid.NewGuid().ToString("N").Substring(0, 8));
            Directory.CreateDirectory(dir);
            string zip = Path.Combine(dir, "payload.zip");
            using (View view = session.Database.OpenView("SELECT `Data` FROM `Binary` WHERE `Name` = 'UboPayload'"))
            {
                view.Execute();
                using (Record record = view.Fetch())
                {
                    if (record == null)
                        throw new InvalidOperationException("the MSI has no UboPayload in its Binary table");
                    record.GetStream(1, zip);
                }
            }
            ZipFile.ExtractToDirectory(zip, dir);
            File.Delete(zip);
            return dir;
        }

        static string BundledZip(string payload)
        {
            string zip = Directory.GetFiles(Path.Combine(payload, "ubo"), "uBlock0_*.chromium.zip").FirstOrDefault();
            return zip ?? throw new InvalidOperationException("the payload has no bundled uBO release");
        }

        // A directory property ends in a backslash, which would escape the
        // closing quote on a command line.
        static string Folder(string directory) => (directory ?? "").TrimEnd('\\');

        static string RunPython(Session session, string payload, List<string> args, out int code)
        {
            var start = new ProcessStartInfo
            {
                FileName = Path.Combine(payload, "python", "python.exe"),
                Arguments = Quote(new[] { Path.Combine(payload, "ubo-lpa.py") }.Concat(args)),
                WorkingDirectory = payload,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardInput = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8,
            };
            start.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
            session.Log($"UboSetup: running {start.FileName} {start.Arguments}");
            var output = new StringBuilder();
            using (var process = new Process { StartInfo = start })
            {
                DataReceivedEventHandler collect = (_, e) =>
                {
                    if (e.Data == null)
                        return;
                    lock (output)
                        output.Append(e.Data).Append('\n');
                };
                process.OutputDataReceived += collect;
                process.ErrorDataReceived += collect;
                process.Start();
                process.StandardInput.Close();
                process.BeginOutputReadLine();
                process.BeginErrorReadLine();
                process.WaitForExit();
                code = process.ExitCode;
            }
            string text;
            lock (output)
                text = output.ToString();
            session.Log($"UboSetup: exit {code}\n{text}");
            return text;
        }

        // Command-line quoting as CommandLineToArgvW reads it.
        static string Quote(IEnumerable<string> args) => string.Join(" ", args.Select(arg =>
        {
            if (arg.Length > 0 && arg.IndexOfAny(new[] { ' ', '\t', '"' }) < 0)
                return arg;
            var quoted = new StringBuilder("\"");
            int backslashes = 0;
            foreach (char c in arg)
            {
                if (c == '\\') { backslashes++; continue; }
                quoted.Append('\\', c == '"' ? backslashes * 2 + 1 : backslashes);
                backslashes = 0;
                quoted.Append(c);
            }
            return quoted.Append('\\', backslashes * 2).Append('"').ToString();
        }));

        static void Progress(Session session, string text)
        {
            using (var record = new Record(3))
            {
                record[1] = "UboPortable";
                record[2] = Escape(text);
                record[3] = "";
                session.Message(InstallMessage.ActionStart, record);
            }
        }

        static void Error(Session session, string text)
        {
            using (var record = new Record(1))
            {
                record.FormatString = "[1]";
                record[1] = text;
                // MB_OK with MB_ICONERROR.
                session.Message((InstallMessage)((int)InstallMessage.Error | 0x10), record);
            }
        }

        static string Tail(string text, int lines = 15) =>
            string.Join("\n", text.TrimEnd().Split('\n').Reverse().Take(lines).Reverse());

        static string Str(Dictionary<string, object> d, string key) =>
            d.TryGetValue(key, out object value) && value != null ? value.ToString() : null;

        // Text shown through a formatted field: [ ] { } would otherwise read
        // as property references and styles.
        static string Escape(string text)
        {
            var escaped = new StringBuilder();
            foreach (char c in text ?? "")
                escaped.Append("[]{}".IndexOf(c) >= 0 ? $"[\\{c}]" : c.ToString());
            return escaped.ToString();
        }

        static void TryDelete(string dir)
        {
            try { Directory.Delete(dir, true); } catch (IOException) { } catch (UnauthorizedAccessException) { }
        }
    }
}
