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

  // ---- apps/desktop/electron-builder-config.mjs ----------------------------
  {
    file: 'apps/desktop/electron-builder-config.mjs',
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
