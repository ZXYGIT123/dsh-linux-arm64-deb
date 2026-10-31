#!/usr/bin/env node
/**
 * Apply the DeepSeek Harness Linux arm64 port to a pinned upstream checkout.
 *
 * Two mechanisms:
 *  1. `overlay/` mirrors upstream paths that are replaced wholesale.
 *  2. `EDITS` below are exact-anchor replacements for small, surgical changes.
 *
 * Every surgical anchor must match exactly once. Any drift (upstream moved on,
 * CRLF checkout, partial apply) aborts the build instead of producing a silently
 * mis-patched package.
 *
 * Usage: node scripts/apply-linux-overlay.mjs [upstream-root]
 */

import { copyFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'

const UPSTREAM = resolve(process.argv[2] ?? 'upstream')
const HERE = import.meta.dirname
const OVERLAY = resolve(HERE, '..', 'overlay')

const EDITS = [
  // ---- apps/desktop/scripts/package-target.ts -------------------------------
  {
    file: 'apps/desktop/scripts/package-target.ts',
    edits: [
      {
        why: 'import the Linux local package environment',
        find: "import { loadDesktopPackageEnvironment, localMacOSPackageEnvironment, validateDesktopPackageEnvironment } from './desktop-package-environment.mjs'",
        replace: "import { loadDesktopPackageEnvironment, localLinuxPackageEnvironment, localMacOSPackageEnvironment, validateDesktopPackageEnvironment } from './desktop-package-environment.mjs'",
      },
      {
        why: 'add linux-arm64 to the fixed target name union',
        find: "export type DesktopPackageTargetName = 'mac-arm64' | 'mac-x64' | 'win-x64'",
        replace: "export type DesktopPackageTargetName = 'mac-arm64' | 'mac-x64' | 'win-x64' | 'linux-arm64'",
      },
      {
        why: 'allow the linux platform and electron-builder --linux selector',
        find: `  readonly platform: 'darwin' | 'win32'
  readonly arch: 'arm64' | 'x64'
  readonly builderPlatform: '--mac' | '--win'`,
        replace: `  readonly platform: 'darwin' | 'win32' | 'linux'
  readonly arch: 'arm64' | 'x64'
  readonly builderPlatform: '--mac' | '--win' | '--linux'`,
      },
      {
        why: 'register the linux-arm64 release target',
        find: `  'win-x64': {
    name: 'win-x64',
    platform: 'win32',
    arch: 'x64',
    builderPlatform: '--win',
    builderArch: '--x64',
  },
}`,
        replace: `  'win-x64': {
    name: 'win-x64',
    platform: 'win32',
    arch: 'x64',
    builderPlatform: '--win',
    builderArch: '--x64',
  },
  'linux-arm64': {
    name: 'linux-arm64',
    platform: 'linux',
    arch: 'arm64',
    builderPlatform: '--linux',
    builderArch: '--arm64',
  },
}`,
      },
      {
        why: 'reject a linux-arm64 build on a non-Linux-arm64 host',
        find: `  if (target.platform === 'win32' && (hostPlatform !== 'win32' || hostArch !== 'x64')) {
    throw new Error('desktop package: win-x64 requires a Windows x64 build host')
  }`,
        replace: `  if (target.platform === 'win32' && (hostPlatform !== 'win32' || hostArch !== 'x64')) {
    throw new Error('desktop package: win-x64 requires a Windows x64 build host')
  }
  if (target.platform === 'linux' && (hostPlatform !== 'linux' || hostArch !== 'arm64')) {
    throw new Error('desktop package: linux-arm64 requires a Linux arm64 build host')
  }`,
      },
      {
        why: 'select the unsigned Linux package environment',
        find: `  const environment = target.platform === 'darwin' && invocation.unsigned
    ? localMacOSPackageEnvironment()
    : loadDesktopPackageEnvironment(target.platform, process.env, APP_ROOT, {`,
        replace: `  const environment = target.platform === 'darwin' && invocation.unsigned
    ? localMacOSPackageEnvironment()
    : target.platform === 'linux'
    ? localLinuxPackageEnvironment(process.env)
    : loadDesktopPackageEnvironment(target.platform, process.env, APP_ROOT, {`,
      },
    ],
  },

  // ---- apps/desktop/scripts/desktop-package-environment.mjs ----------------
  {
    file: 'apps/desktop/scripts/desktop-package-environment.mjs',
    edits: [
      {
        why: 'provide a local unsigned Linux packaging environment',
        find: `/**
 * Read a machine preset when selected, otherwise the target's required dotenv; no ambient release fallback.`,
        replace: `/**
 * Select local Linux unsigned build defaults without reading dotenv files or inheriting release settings.
 * The Linux community build is unsigned-only, so no update, policy or signing settings are collected.
 * @param {NodeJS.ProcessEnv} environment Parent environment, retained for build tools and networking.
 * @returns {NodeJS.ProcessEnv} Unsigned build environment with the standard application identifier.
 */
export function localLinuxPackageEnvironment(environment = process.env) {
  return {
    ...Object.fromEntries(Object.entries(environment).filter(([name]) => !AMBIENT_RELEASE_SETTING.test(name))),
    DSH_DESKTOP_APP_ID: 'com.deepseek.harness',
    DSH_DESKTOP_UNSIGNED: '1',
  }
}

/**
 * Read a machine preset when selected, otherwise the target's required dotenv; no ambient release fallback.`,
      },
      {
        why: 'validate Linux unsigned builds without macOS/Windows signing settings',
        find: `  resolveDesktopAppId(environment)
  resolveNpmRegistry(environment)
  if (target.platform !== 'darwin' || !options.unsigned) resolveDesktopPolicyEnvironment(environment)`,
        replace: `  resolveDesktopAppId(environment)
  resolveNpmRegistry(environment)
  if (target.platform === 'linux') {
    if (!options.unsigned) throw new Error('desktop package: linux targets support unsigned builds only')
    return
  }
  if (target.platform !== 'darwin' || !options.unsigned) resolveDesktopPolicyEnvironment(environment)`,
      },
    ],
  },

  // ---- apps/desktop/scripts/desktop-toolchain-preflight.ts ------------------
  {
    file: 'apps/desktop/scripts/desktop-toolchain-preflight.ts',
    edits: [
      {
        why: 'probe the toolchain for a Linux target as well',
        find: `export async function probeDesktopToolchain(
  platform: 'darwin' | 'win32',`,
        replace: `export async function probeDesktopToolchain(
  platform: 'darwin' | 'win32' | 'linux',`,
      },
      {
        why: 'accept a Linux target in the required-toolchain gate',
        find: `export async function requireDesktopToolchain(
  platform: 'darwin' | 'win32',`,
        replace: `export async function requireDesktopToolchain(
  platform: 'darwin' | 'win32' | 'linux',`,
      },
    ],
  },

  // ---- apps/desktop/scripts/prepare-runtime.ts ------------------------------
  {
    file: 'apps/desktop/scripts/prepare-runtime.ts',
    edits: [
      {
        why: 'locate the extracted Linux Electron executable',
        find: `  const executable = join(BUILD_PATHS.electron, platform === 'win32' ? 'electron.exe' : 'Electron.app/Contents/MacOS/Electron')`,
        replace: `  const executable = join(BUILD_PATHS.electron, platform === 'win32' ? 'electron.exe' : platform === 'linux' ? 'electron' : 'Electron.app/Contents/MacOS/Electron')`,
      },
    ],
  },

  // ---- apps/desktop/scripts/prepare-dsh.ts (Office engine probe) ----------
  // Electron's ASAR filesystem returns null from lstatSync({throwIfNoEntry:false}) instead of
  // the documented undefined, so LibreOfficeKit's presence probe reports an absent Linux
  // engine as "installed but incomplete" and aborts every conversion. Restoring Node's
  // semantics lets the resolver fall back to the bundled WASM engine, as upstream intends
  // for Linux. See analysis/port-report.md.
  {
    file: 'apps/desktop/scripts/prepare-dsh.ts',
    edits: [
      {
        why: 'patch the installed LibreOfficeKit lstat presence probe for ASAR semantics',
        find: `function manifestVersion(path: string, subject: string): string {`,
        replace: `/**
 * Electron's ASAR filesystem returns null (not undefined) from lstatSync({ throwIfNoEntry: false }),
 * which makes LibreOfficeKit treat an absent Linux engine package as installed-but-incomplete and
 * abort every conversion. Restore the documented Node semantics so the resolver falls back to the
 * bundled WASM engine, which is the engine LibreOfficeKit ships for Linux by default.
 * @param modules - Installed production node_modules directory.
 */
function patchOfficeEngineProbe(modules: string): void {
  const file = join(modules, '@deepseek-ai', 'libreoffice-kit', 'lib', 'index.js')
  const source = readFileSync(file, 'utf8')
  const probe = 'lstatSync(join(directory, name), { throwIfNoEntry: false }) !== void 0'
  if (source.split(probe).length - 1 !== 1) {
    throw new Error('desktop runtime: LibreOfficeKit engine probe changed; refusing to patch the installed package')
  }
  writeFileSync(file, source.replace(probe, 'lstatSync(join(directory, name), { throwIfNoEntry: false }) != null'))
}

function manifestVersion(path: string, subject: string): string {`,
      },
      {
        why: 'apply the probe fix to the freshly installed runtime before materializing it',
        find: `    await packagingStep(process.env.DSH_DESKTOP_PACKAGING_RUN_DIR, 'runtime:install', () => runPnpm(['install', '--prod', '--frozen-lockfile', '--trust-lockfile']))`,
        replace: `    await packagingStep(process.env.DSH_DESKTOP_PACKAGING_RUN_DIR, 'runtime:install', () => runPnpm(['install', '--prod', '--frozen-lockfile', '--trust-lockfile']))
    await packagingStep(process.env.DSH_DESKTOP_PACKAGING_RUN_DIR, 'runtime:office-engine-probe', async () => {
      patchOfficeEngineProbe(join(BUILD_ROOT, 'node_modules'))
    })`,
      },
    ],
  },

  // ---- apps/desktop/scripts/prepare-dsh.ts ---------------------------------
  {
    file: 'apps/desktop/scripts/prepare-dsh.ts',
    edits: [
      {
        why: 'locate the extracted Linux Electron Node runtime',
        find: `const NODE = join(BUILD_PATHS.electron, process.platform === 'win32' ? 'electron.exe' : 'Electron.app/Contents/MacOS/Electron')`,
        replace: `const NODE = join(BUILD_PATHS.electron, process.platform === 'win32' ? 'electron.exe' : process.platform === 'linux' ? 'electron' : 'Electron.app/Contents/MacOS/Electron')`,
      },
    ],
  },

  // ---- apps/desktop/scripts/prepare-dsh.ts (connection network watch) -----
  // Wayland containers without NetworkManager/D-Bus flap the browser offline/online state.
  // The client connection aborts its generation on such a hint, so a healthy loopback
  // connection cycles "reconnecting / connected" every couple of seconds. This payload only
  // talks to its own Host, so every copy of the client-connection offline listener becomes a
  // no-op. The patch runs after materialization so nested duplicate copies are covered too.
  {
    file: 'apps/desktop/scripts/prepare-dsh.ts',
    edits: [
      {
        why: 'read runtime directories recursively for the connection patch',
        find: `import { copyFileSync, cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'`,
        replace: `import { copyFileSync, cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs'`,
      },
      {
        why: 'add the connection offline-listener neutralizer',
        find: `function patchOfficeEngineProbe(modules: string): void {`,
        replace: `/**
 * Disable every copy of the browser offline listener in the materialized client-connection
 * bundle. Wayland containers without NetworkManager/D-Bus report spurious offline/online flips,
 * and the connection layer aborts the current generation on any offline hint, so a healthy
 * local connection would cycle through "reconnecting / connected" indefinitely.
 * @param modules - Materialized production node_modules directory.
 */
function patchConnectionNetworkWatch(modules: string): void {
  let patched = 0
  const visit = (directory: string): void => {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const path = join(directory, entry.name)
      if (entry.isDirectory()) { visit(path); continue }
      if (!entry.isFile() || !path.includes('dsh-client-connection') || !path.endsWith('client.browser.js')) continue
      let source = readFileSync(path, 'utf8')
      const probe = 'browser.addEventListener("offline", offline);'
      if (source.split(probe).length - 1 !== 1) continue
      source = source.replace(probe, 'browser.addEventListener("offline", () => {});')
      // Diagnostic: report why a generation ended (connection close reason or handshake error).
      const retryProbe = /(\\n[\\t ]+)await failed;/
      if (source.match(retryProbe) === null) {
        throw new Error('desktop runtime: unexpected connection retry loop; refusing to patch')
      }
      source = source.replace(retryProbe, '$1await failed;\\n$1const reason = ac.signal.reason;\\n$1console.error("[connection] generation ended:", reason && (reason.name || reason.message) ? String(reason.name) + ": " + String(reason.message) + " code=" + String(reason.code) : String(reason));')
      // Diagnostic: a bare AbortError means the owner was released; print the caller stack.
      // Older duplicate bundles may not carry this helper, so tracing stays optional.
      const releaseProbe = /(const releaseOwner = \(current\) => \{\s*if \(owner !== current\) return;)/
      if (source.match(releaseProbe) !== null) {
        source = source.replace(releaseProbe, '$1\\n\\t\\t\\t\\tconsole.trace("[connection] owner released");')
      }
      writeFileSync(path, source)
      patched += 1
    }
  }
  visit(modules)
  if (patched === 0) throw new Error('desktop runtime: no connection offline listener found to patch')
  console.log('desktop runtime: neutralized ' + String(patched) + ' browser offline listener(s)')
}

function patchOfficeEngineProbe(modules: string): void {`,
      },
      {
        why: 'apply the offline-listener fix to the materialized runtime',
        find: `    await packagingStep(process.env.DSH_DESKTOP_PACKAGING_RUN_DIR, 'runtime:materialize-modules', async () => {
      cpSync(modules, join(DSH_OUTPUT_ROOT, 'node_modules'), {
        recursive: true, dereference: true,
        filter: source => desktopRuntimeFileExclusion(relative(modules, source), target, officeEngine) === undefined,
      })
    })`,
        replace: `    await packagingStep(process.env.DSH_DESKTOP_PACKAGING_RUN_DIR, 'runtime:materialize-modules', async () => {
      cpSync(modules, join(DSH_OUTPUT_ROOT, 'node_modules'), {
        recursive: true, dereference: true,
        filter: source => desktopRuntimeFileExclusion(relative(modules, source), target, officeEngine) === undefined,
      })
    })
    await packagingStep(process.env.DSH_DESKTOP_PACKAGING_RUN_DIR, 'runtime:connection-network-watch', async () => {
      patchConnectionNetworkWatch(join(DSH_OUTPUT_ROOT, 'node_modules'))
    })`,
      },
    ],
  },

  // ---- apps/desktop/scripts/desktop-auto-update-environment.mjs -------------
  {
    file: 'apps/desktop/scripts/desktop-auto-update-environment.mjs',
    edits: [
      {
        why: 'recognise the linux-arm64 build record target',
        find: `const UPDATE_TARGETS = new Set(['mac-arm64', 'mac-x64', 'win-x64'])`,
        replace: `const UPDATE_TARGETS = new Set(['mac-arm64', 'mac-x64', 'win-x64', 'linux-arm64'])`,
      },
    ],
  },
  {
    file: 'apps/desktop/scripts/desktop-auto-update-environment.d.mts',
    edits: [
      {
        why: 'type the linux-arm64 build record target',
        find: `export type DesktopAutoUpdateTarget = 'mac-arm64' | 'mac-x64' | 'win-x64'`,
        replace: `export type DesktopAutoUpdateTarget = 'mac-arm64' | 'mac-x64' | 'win-x64' | 'linux-arm64'`,
      },
    ],
  },

  // ---- apps/desktop/src/main.ts -------------------------------------------
  // Runtime-only fixes: the shell runs on Linux once packaging succeeds, but three
  // platform assumptions make the packaged app unusable (see analysis/port-report.md).
  {
    file: 'apps/desktop/src/main.ts',
    edits: [
      {
        why: 'never resolve a mandatory-update policy on Linux (its platform whitelist throws)',
        find: `  const policyConfig = resolveDesktopPolicyConfig(policyInput, !app.isPackaged)`,
        replace: `  // This build publishes no mandatory-update policy service for Linux, and the policy
  // implementation only whitelists win32/darwin: never construct it here.
  const policyConfig = process.platform === 'linux' ? undefined : resolveDesktopPolicyConfig(policyInput, !app.isPackaged)`,
      },
      {
        why: 'hide the update-check menu entry on Linux (electron-updater has no deb feed)',
        find: `    { label: currentDesktopLocale().messages.checkUpdatesMenu, click: () => { void openUpdatePrompt(true) } },`,
        replace: `    ...process.platform === 'linux' ? [] : [{ label: currentDesktopLocale().messages.checkUpdatesMenu, click: () => { void openUpdatePrompt(true) } }],`,
      },
      {
        why: 'skip automatic update/policy checks on Linux',
        find: `  const automaticCheck = (): void => {
    if (!quitting) void mandatoryPolicy?.check('foreground-or-resume').catch((error: unknown) => { console.error(error) })`,
        replace: `  const automaticCheck = (): void => {
    // No update feed and no policy service exist for this Linux build; both checks could only fail.
    if (process.platform === 'linux') return
    if (!quitting) void mandatoryPolicy?.check('foreground-or-resume').catch((error: unknown) => { console.error(error) })`,
      },
      {
        why: 'closing the main window quits on Linux, which has no tray to restore it',
        find: `      if (quitting || shellInstallerOwnsQuit || sessionEnding) return
      event.preventDefault()
      if (updateDialog.isOpen) { updateDialog.focus(); return }`,
        replace: `      if (quitting || shellInstallerOwnsQuit || sessionEnding) return
      // Linux has no tray (Windows-only below), so hiding the window would leave a running
      // process with no way back: closing the main window quits the application there.
      if (process.platform === 'linux') { quitWithoutConfirmation(); return }
      event.preventDefault()
      if (updateDialog.isOpen) { updateDialog.focus(); return }`,
      },
    ],
  },

  // ---- scripts/libreoffice-packages.mjs ------------------------------------
  // Linux engines are published per libc (linux-arm64-glibc / linux-arm64-musl); the
  // plain platform-arch lookup would silently select WASM and then drop the native
  // engine from the packaged runtime.
  {
    file: 'scripts/libreoffice-packages.mjs',
    edits: [
      {
        why: 'select the installed Linux engine variant instead of falling back to WASM',
        find: `export function selectOfficeEngine(manifest, target) {
  const native = \`\${target.platform}-\${target.arch}\`
  return Object.hasOwn(manifest.optionalDependencies ?? {}, \`@deepseek-ai/libreoffice-kit-\${native}\`) ? native : 'wasm'
}`,
        replace: `export function selectOfficeEngine(manifest, target) {
  const optional = manifest.optionalDependencies ?? {}
  const native = \`\${target.platform}-\${target.arch}\`
  if (Object.hasOwn(optional, \`@deepseek-ai/libreoffice-kit-\${native}\`)) return native
  if (target.platform === 'linux') {
    // Debian/Ubuntu packages run on glibc; the native engine is preferred when the kit
    // declares the variant, and WASM remains the portable fallback.
    for (const variant of [\`\${native}-glibc\`, \`\${native}-musl\`]) {
      if (Object.hasOwn(optional, \`@deepseek-ai/libreoffice-kit-\${variant}\`)) return variant
    }
  }
  return 'wasm'
}`,
      },
    ],
  },

  // ---- apps/desktop/scripts/desktop-upload-plan.ts -------------------------
  // The upload plan is keyed by the fixed target-name union; the Linux target never uploads,
  // but the record must stay exhaustive for the aggregate type check.
  {
    file: 'apps/desktop/scripts/desktop-upload-plan.ts',
    edits: [
      {
        why: 'keep the fixed-target upload plan exhaustive after adding linux-arm64',
        find: `  'win-x64': { platform: 'win32', arch: 'x64', os: 'win' },
} as const satisfies Record<DesktopPackageTargetName, {`,
        replace: `  'win-x64': { platform: 'win32', arch: 'x64', os: 'win' },
  'linux-arm64': { platform: 'linux', arch: 'arm64', os: 'linux' },
} as const satisfies Record<DesktopPackageTargetName, {`,
      },
    ],
  },

  // ---- apps/desktop/scripts/retain-desktop-ci.ts ---------------------------
  {
    file: 'apps/desktop/scripts/retain-desktop-ci.ts',
    edits: [
      {
        why: 'retention only understands the published macOS/Windows targets',
        find: `  const target = resolveDesktopBuildTarget(env)
  const windows = target === 'win-x64'`,
        replace: `  const target = resolveDesktopBuildTarget(env)
  // The Linux community build publishes no update feed, so it has nothing to retain.
  if (target === 'linux-arm64') throw new Error('desktop retention: linux-arm64 has no updater release to retain')
  const windows = target === 'win-x64'`,
      },
    ],
  },

  // ---- apps/desktop/scripts/electron-builder-config.mjs --------------------
  {
    file: 'apps/desktop/scripts/electron-builder-config.mjs',
    edits: [
      {
        why: 'Linux builds are unsigned and need no mandatory-update policy service',
        find: `  const policy = packagesMacOS && unsigned ? undefined : resolveDesktopPolicyEnvironment(env)`,
        replace: `  const policy = (packagesMacOS && unsigned) || resolvedPlatform === 'linux' ? undefined : resolveDesktopPolicyEnvironment(env)`,
      },
      {
        why: 'Linux builds do not publish electron-updater feeds',
        find: `  const update = unsigned ? undefined : resolveDesktopAutoUpdateConfig(env, resolvedPlatform, resolvedArch)`,
        replace: `  const update = unsigned || resolvedPlatform === 'linux' ? undefined : resolveDesktopAutoUpdateConfig(env, resolvedPlatform, resolvedArch)`,
      },
      {
        why: 'package the Linux target as a deb with an explicit executable name and icon',
        find: `    linux: {
      category: 'Development',
      target: ['AppImage'],
    },`,
        replace: `    linux: {
      category: 'Development',
      target: ['deb'],
      executableName: 'deepseek-harness',
      icon: fileURLToPath(new URL('../resources/icon.png', import.meta.url)),
      maintainer: 'DeepSeek Harness Linux community build <dsh-linux@users.noreply.github.com>',
      synopsis: 'DeepSeek Harness',
      description: 'DeepSeek Harness desktop shell with a bundled dsh runtime.',
    },`,
      },
    ],
  },
]

function listOverlayFiles(root) {
  const files = []
  const visit = (directory) => {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const path = join(directory, entry.name)
      if (entry.isDirectory()) visit(path)
      else files.push(path)
    }
  }
  visit(root)
  return files
}

import { readdirSync } from 'node:fs'

function applyOverlay() {
  let copied = 0
  for (const source of listOverlayFiles(OVERLAY)) {
    const relative = source.slice(OVERLAY.length + 1).split(/[\\/]/).join('/')
    const destination = join(UPSTREAM, ...relative.split('/'))
    mkdirSync(dirname(destination), { recursive: true })
    copyFileSync(source, destination)
    copied += 1
  }
  return copied
}

function applyEdits() {
  const failures = []
  let applied = 0
  for (const { file, edits } of EDITS) {
    const path = join(UPSTREAM, ...file.split('/'))
    if (!existsSync(path)) {
      failures.push(`${file}: missing from checkout`)
      continue
    }
    let source = readFileSync(path, 'utf8').replaceAll('\r\n', '\n')
    for (const { why, find, replace } of edits) {
      const occurrences = source.split(find).length - 1
      if (occurrences !== 1) {
        failures.push(`${file}: expected exactly one match for "${why}", found ${occurrences}`)
        continue
      }
      source = source.replace(find, replace)
      applied += 1
    }
    writeFileSync(path, source)
  }
  return { applied, failures }
}

const copied = applyOverlay()
const { applied, failures } = applyEdits()

console.log(`linux overlay: copied ${copied} replacement file(s), applied ${applied} surgical edit(s) to ${UPSTREAM}`)
if (failures.length > 0) {
  for (const failure of failures) console.error(`linux overlay: FAILED ${failure}`)
  process.exit(1)
}
