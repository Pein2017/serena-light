#!/usr/bin/env node

// Pyright 1.1.403 private attribution seam. Every version, bundle hash,
// webpack module id, and required AnalyzerService method is checked before the
// native CLI is allowed to provide production scope evidence.

import { createHash } from "node:crypto";
import { readFileSync, realpathSync, writeSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, isAbsolute, join, resolve } from "node:path";
import process from "node:process";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

export const EXPECTED_BUNDLE = Object.freeze({
  version: "1.1.403",
  pyrightJsSha256: "63eb400b789e1f83b41e3d433c7b6091573ecdf5bb2471a8b2bd8b63a06011bc",
  pyrightInternalJsSha256: "37b8aa0e50a2d8136d192e13d5dbbe2e889b85d29cfa963c9c470a5b158d2c44",
});

const BOOTSTRAP_MARKER = "o.x()})();";
const WEBPACK_REQUIRE_GLOBAL = "__serenaLightPyrightWebpackRequire";
const ANALYZER_SERVICE_MODULE_ID = 2948;
const CLI_MAIN_MODULE_ID = 9323;

export class ProbeError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "ProbeError";
    this.code = code;
  }
}

function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

function assertSingleMarker(source, marker) {
  const first = source.indexOf(marker);
  if (first < 0 || source.indexOf(marker, first + marker.length) >= 0) {
    throw new ProbeError(
      "PYRIGHT_BOOTSTRAP_STRUCTURE_MISMATCH",
      "pinned pyright.js webpack bootstrap marker is missing or ambiguous",
    );
  }
}

export function validateBundleEvidence(
  { packageJsonBytes, pyrightJsBytes, pyrightInternalJsBytes },
  expected = EXPECTED_BUNDLE,
) {
  let packageJson;
  try {
    packageJson = JSON.parse(packageJsonBytes.toString("utf8"));
  } catch {
    throw new ProbeError("PYRIGHT_PACKAGE_JSON_INVALID", "Pyright package.json is not valid JSON");
  }
  if (packageJson.version !== expected.version) {
    throw new ProbeError(
      "PYRIGHT_VERSION_MISMATCH",
      `Pyright version ${JSON.stringify(packageJson.version)} does not match ${expected.version}`,
    );
  }
  const pyrightJsSha256 = sha256(pyrightJsBytes);
  if (pyrightJsSha256 !== expected.pyrightJsSha256) {
    throw new ProbeError(
      "PYRIGHT_JS_HASH_MISMATCH",
      `pyright.js SHA-256 ${pyrightJsSha256} does not match the pinned bundle`,
    );
  }
  const pyrightInternalJsSha256 = sha256(pyrightInternalJsBytes);
  if (pyrightInternalJsSha256 !== expected.pyrightInternalJsSha256) {
    throw new ProbeError(
      "PYRIGHT_INTERNAL_JS_HASH_MISMATCH",
      `pyright-internal.js SHA-256 ${pyrightInternalJsSha256} does not match the pinned bundle`,
    );
  }
  return {
    version: packageJson.version,
    packageJsonSha256: sha256(packageJsonBytes),
    pyrightJsSha256,
    pyrightInternalJsSha256,
  };
}

export function rewriteBootstrapSource(source) {
  assertSingleMarker(source, BOOTSTRAP_MARKER);
  return source.replace(BOOTSTRAP_MARKER, `globalThis.${WEBPACK_REQUIRE_GLOBAL}=o})();`);
}

export function assertRequiredBundleModules(webpackRequire) {
  let analyzerModule;
  let cliModule;
  try {
    analyzerModule = webpackRequire(ANALYZER_SERVICE_MODULE_ID);
    cliModule = webpackRequire(CLI_MAIN_MODULE_ID);
  } catch (error) {
    throw new ProbeError(
      "PYRIGHT_MODULE_STRUCTURE_MISMATCH",
      `required pinned Pyright module could not load: ${error instanceof Error ? error.message : String(error)}`,
    );
  }
  const AnalyzerService = analyzerModule?.AnalyzerService;
  if (
    typeof AnalyzerService !== "function" ||
    typeof AnalyzerService.prototype.setOptions !== "function" ||
    typeof AnalyzerService.prototype.getOwnedFiles !== "function" ||
    typeof AnalyzerService.prototype.getConfigOptions !== "function" ||
    typeof cliModule?.main !== "function"
  ) {
    throw new ProbeError(
      "PYRIGHT_MODULE_STRUCTURE_MISMATCH",
      "required AnalyzerService/setOptions/getOwnedFiles/getConfigOptions or native CLI main export is absent",
    );
  }
  return { AnalyzerService, cliMain: cliModule.main };
}

function locatePinnedPackage() {
  const executable = realpathSync(process.execPath);
  const runtimeRoot = resolve(dirname(executable), "../..");
  const packageRoot = resolve(runtimeRoot, "node-packages/node_modules/pyright");
  if (!packageRoot.startsWith(`${runtimeRoot}/`)) {
    throw new ProbeError("PYRIGHT_PACKAGE_PATH_INVALID", "derived Pyright package escaped the fixed runtime root");
  }
  return { executable, packageRoot };
}

function loadPinnedRuntime(paths, bytes) {
  if (globalThis[WEBPACK_REQUIRE_GLOBAL] !== undefined) {
    throw new ProbeError("PYRIGHT_BOOTSTRAP_STRUCTURE_MISMATCH", "webpack runtime global already exists");
  }
  const dist = join(paths.packageRoot, "dist");
  const nativeRequire = createRequire(join(dist, "pyright.js"));
  globalThis.require = nativeRequire;
  globalThis.__rootDirectory = `${dist}/`;
  vm.runInThisContext(rewriteBootstrapSource(bytes.pyrightJsBytes.toString("utf8")), {
    filename: join(dist, "pyright.js"),
  });
  const webpackRequire = globalThis[WEBPACK_REQUIRE_GLOBAL];
  if (typeof webpackRequire !== "function" || typeof webpackRequire.m !== "object") {
    throw new ProbeError("PYRIGHT_BOOTSTRAP_STRUCTURE_MISMATCH", "webpack module loader was not exposed");
  }
  for (const chunkName of ["vendor.js", "pyright-internal.js"]) {
    const chunk = nativeRequire(join(dist, chunkName));
    if (!chunk || !Array.isArray(chunk.ids) || typeof chunk.modules !== "object") {
      throw new ProbeError(
        "PYRIGHT_MODULE_STRUCTURE_MISMATCH",
        `${chunkName} does not expose the expected webpack chunk structure`,
      );
    }
    Object.assign(webpackRequire.m, chunk.modules);
  }
  return assertRequiredBundleModules(webpackRequire);
}

function ownedPathList(service) {
  const owned = service.getOwnedFiles();
  if (!Array.isArray(owned)) {
    throw new ProbeError("PYRIGHT_OWNED_FILES_INVALID", "AnalyzerService.getOwnedFiles did not return an array");
  }
  const paths = owned.map((uri) => {
    if (!uri || typeof uri.getFilePath !== "function") {
      throw new ProbeError("PYRIGHT_OWNED_FILES_INVALID", "owned file entry is not a native Pyright file URI");
    }
    const filePath = uri.getFilePath();
    if (typeof filePath !== "string" || !isAbsolute(filePath) || filePath.includes("\0")) {
      throw new ProbeError("PYRIGHT_OWNED_FILES_INVALID", "owned file URI did not produce a safe absolute path");
    }
    return filePath;
  });
  paths.sort();
  if (new Set(paths).size !== paths.length) {
    throw new ProbeError("PYRIGHT_OWNED_FILES_INVALID", "native owned file list contains duplicate paths");
  }
  return paths;
}

function selectedProjectEvidence(service) {
  const configOptions = service.getConfigOptions();
  if (!configOptions || typeof configOptions !== "object") {
    throw new ProbeError("PYRIGHT_CONFIG_OPTIONS_INVALID", "AnalyzerService.getConfigOptions returned no object");
  }
  const configFileSource = configOptions.configFileSource;
  if (configFileSource === undefined) {
    return {
      selected_config_path: null,
      project_kind: "workspace_default",
      attribution: "AnalyzerService.getConfigOptions().configFileSource after native CLI setOptions",
    };
  }
  if (!configFileSource || typeof configFileSource.getFilePath !== "function") {
    throw new ProbeError("PYRIGHT_CONFIG_OPTIONS_INVALID", "native configFileSource is not a Pyright file URI");
  }
  const configPath = configFileSource.getFilePath();
  if (typeof configPath !== "string" || !isAbsolute(configPath) || configPath.includes("\0")) {
    throw new ProbeError("PYRIGHT_CONFIG_OPTIONS_INVALID", "native configFileSource did not produce a safe absolute path");
  }
  return {
    selected_config_path: configPath,
    project_kind: "configured",
    attribution: "AnalyzerService.getConfigOptions().configFileSource after native CLI setOptions",
  };
}

function writeAllSync(fd, text) {
  const bytes = Buffer.from(text, "utf8");
  const retryCell = new Int32Array(new SharedArrayBuffer(4));
  const deadline = Date.now() + 10_000;
  let offset = 0;
  while (offset < bytes.length) {
    let written;
    try {
      written = writeSync(fd, bytes, offset, bytes.length - offset);
    } catch (error) {
      if (error && (error.code === "EAGAIN" || error.code === "EWOULDBLOCK") && Date.now() < deadline) {
        Atomics.wait(retryCell, 0, 0, 1);
        continue;
      }
      throw error;
    }
    if (written <= 0) {
      throw new ProbeError("PYRIGHT_PROBE_WRITE_FAILED", `file descriptor ${fd} accepted no output bytes`);
    }
    offset += written;
  }
}

function terminateWithError(error) {
  const code = error instanceof ProbeError ? error.code : "PYRIGHT_PROBE_FAILED";
  const message = error instanceof Error ? error.message : String(error);
  writeAllSync(2, `pyright-owned-files probe failed: ${code}: ${message}\n`);
  process.exit(1);
}

export async function runProbe(cliArguments) {
  const started = process.hrtime.bigint();
  try {
    const paths = locatePinnedPackage();
    const packageJsonPath = join(paths.packageRoot, "package.json");
    const pyrightJsPath = join(paths.packageRoot, "dist/pyright.js");
    const pyrightInternalJsPath = join(paths.packageRoot, "dist/pyright-internal.js");
    const bytes = {
      packageJsonBytes: readFileSync(packageJsonPath),
      pyrightJsBytes: readFileSync(pyrightJsPath),
      pyrightInternalJsBytes: readFileSync(pyrightInternalJsPath),
    };
    const evidence = validateBundleEvidence(bytes);
    const { AnalyzerService, cliMain } = loadPinnedRuntime(paths, bytes);
    const originalSetOptions = AnalyzerService.prototype.setOptions;
    let captured = false;
    AnalyzerService.prototype.setOptions = function nativeSetOptionsAndCapture(options) {
      try {
        originalSetOptions.call(this, options);
        if (captured) {
          throw new ProbeError("PYRIGHT_SET_OPTIONS_REPEATED", "native CLI invoked setOptions more than once");
        }
        captured = true;
        const ownedFiles = ownedPathList(this);
        const report = {
          schema_version: 1,
          attribution: "AnalyzerService.getOwnedFiles after native CLI setOptions",
          owned_files: ownedFiles,
          owned_file_count: ownedFiles.length,
          owned_files_sha256: sha256(Buffer.from(ownedFiles.join("\0"), "utf8")),
          project: selectedProjectEvidence(this),
          engine: {
            name: "pyright",
            version: evidence.version,
            package_root: paths.packageRoot,
            cli_entrypoint: join(paths.packageRoot, "index.js"),
            node: paths.executable,
            cli_arguments: [...cliArguments],
          },
          bundle: {
            package_json: { path: packageJsonPath, sha256: evidence.packageJsonSha256 },
            pyright_js: { path: pyrightJsPath, sha256: evidence.pyrightJsSha256 },
            pyright_internal_js: { path: pyrightInternalJsPath, sha256: evidence.pyrightInternalJsSha256 },
          },
          elapsed_seconds: Number((Number(process.hrtime.bigint() - started) / 1e9).toFixed(3)),
          max_rss_kib: process.resourceUsage().maxRSS,
        };
        writeAllSync(1, `${JSON.stringify(report)}\n`);
        process.exit(0);
      } catch (error) {
        terminateWithError(error);
      }
    };
    process.argv = [paths.executable, join(paths.packageRoot, "index.js"), ...cliArguments];
    const exitStatus = await cliMain();
    if (!captured) {
      throw new ProbeError(
        "PYRIGHT_SET_OPTIONS_NOT_REACHED",
        `native Pyright CLI exited with status ${String(exitStatus)} before setOptions attribution`,
      );
    }
  } catch (error) {
    terminateWithError(error);
  }
}

const isMain = process.argv[1] && resolve(process.argv[1]) === resolve(fileURLToPath(import.meta.url));
if (isMain) {
  await runProbe(process.argv.slice(2));
}
