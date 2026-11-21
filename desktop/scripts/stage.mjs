import { execFileSync } from "node:child_process";
import { cpSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const desktop = fileURLToPath(new URL("../", import.meta.url));
const repository = path.resolve(desktop, "..");
const staging = path.join(desktop, ".stage");
const runtime = path.join(staging, "runtime");
const workbench = path.join(runtime, "workbench");
const pythonDirectory = path.join(runtime, "python");
const pythonVersion = "3.12.14";
const pythonOS = { darwin: "macos", win32: "windows", linux: "linux" }[process.platform];
const pythonArch = { arm64: "aarch64", x64: "x86_64" }[process.arch];
if (!pythonOS || !pythonArch) throw new Error(`Unsupported build host: ${process.platform}/${process.arch}`);
const pythonRequest = `cpython-${pythonVersion}-${pythonOS}-${pythonArch}-${pythonOS === "linux" ? "gnu" : "none"}`;
const environment = {
  ...process.env,
  ...(process.platform === "darwin" ? { MACOSX_DEPLOYMENT_TARGET: "14.0" } : {}),
};

function run(command, args, options = {}) {
  return execFileSync(command, args, {
    cwd: repository, env: environment, encoding: "utf8", stdio: "inherit", ...options,
  });
}

run("uv", ["python", "install", "--no-bin", pythonRequest]);
const managedPython = run("uv", [
  "python", "find", "--managed-python", "--system", "--no-project", pythonRequest,
], { stdio: ["ignore", "pipe", "inherit"] }).trim();
const info = JSON.parse(run(managedPython, ["-I", "-c", [
  "import json, pathlib, sys, sysconfig",
  "base = pathlib.Path(sys.base_prefix)",
  "print(json.dumps({",
  '    "base": str(base),',
  '    "python": str(pathlib.Path(sys.executable).relative_to(base)),',
  '    "packages": str(pathlib.Path(sysconfig.get_path("purelib")).relative_to(base)),',
  '    "platform": sysconfig.get_platform(),',
  "}))",
].join("\n")], { stdio: ["ignore", "pipe", "inherit"] }));

rmSync(staging, { recursive: true, force: true });
mkdirSync(workbench, { recursive: true });
cpSync(info.base, pythonDirectory, { recursive: true, verbatimSymlinks: true });
const python = path.join(pythonDirectory, info.python);
const packages = path.join(pythonDirectory, info.packages);
rmSync(packages, { recursive: true, force: true });
mkdirSync(packages, { recursive: true });

const sourceWheels = process.platform === "win32" && process.arch === "arm64"
  ? ["--no-binary", "pyclipper"]
  : [];
run("uv", [
  "pip", "install", "--python", managedPython, "--target", packages,
  "--link-mode", "copy", "--only-binary", ":all:", ...sourceWheels, "--require-hashes",
  "-r", path.join(desktop, "python-requirements.txt"),
]);

// Stage only repository paths used by the workstation and its templates.
for (const entry of ["server", "subskills", "utils", "references", "index.json", "version.json", "SKILL.md", "LICENSE"]) {
  cpSync(path.join(repository, entry), path.join(workbench, entry), {
    recursive: true,
    filter: (source) => {
      const name = path.basename(source);
      return !name.startsWith(".") && name !== "__pycache__" && name !== "evals" && !name.endsWith(".pyc");
    },
  });
}
cpSync(path.join(desktop, "local_service.py"), path.join(workbench, "desktop_service.py"));
writeFileSync(path.join(runtime, "runtime.json"), JSON.stringify({
  python: path.join("python", info.python), platform: info.platform, version: pythonVersion,
}));

const icons = path.join(staging, "icons");
mkdirSync(icons, { recursive: true });
cpSync(path.join(repository, "assets", "logo.png"), path.join(icons, "icon.png"));

// Resolve the OCR weights while packaging so a first upload can work offline.
run(python, ["-I", "-c", "from rapidocr import RapidOCR; RapidOCR()"], { cwd: workbench });
console.log(`Staged Python ${pythonVersion} and the workstation for ${info.platform}.`);
