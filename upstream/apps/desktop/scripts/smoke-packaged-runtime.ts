/** Validate the assembled application, including native Office conversion outside ASAR. */
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { parseArgs } from 'node:util'
import { desktopArtifactOutput, resolveDesktopBuildTarget, resolveDesktopTargetBuildPaths } from './desktop-build-paths.mjs'
import { readDesktopRuntime, verifyDesktopRuntime } from '../src/runtime-tree.ts'
import { verifyWindowsCode } from './windows-runtime-signature.mjs'
import { smokePreparedRuntime } from './smoke-prepared-runtime.ts'
import { resolveDesktopPackageTarget } from './package-target.ts'

const paths = resolveDesktopTargetBuildPaths()
const { values } = parseArgs({ options: { unsigned: { type: 'boolean', default: false }, 'skip-notarization': { type: 'boolean', default: false } }, allowPositionals: false })
const target = resolveDesktopBuildTarget()
const windows = target === 'win-x64'
const linux = target === 'linux-arm64'
const artifacts = desktopArtifactOutput(paths, { unsigned: values.unsigned, skipNotarization: values['skip-notarization'] }).directory
const application = windows ? join(artifacts, 'win-unpacked')
  : linux ? join(artifacts, 'linux-arm64-unpacked')
  : join(artifacts, target === 'mac-arm64' ? 'mac-arm64' : 'mac', 'DeepSeek Harness.app', 'Contents')
const resources = windows || linux ? join(application, 'resources') : join(application, 'Resources')
const executable = windows ? join(application, 'DeepSeek Harness.exe')
  : linux ? join(application, 'deepseek-harness')
  : join(application, 'MacOS', 'DeepSeek Harness')

/**
 * Print the Office engine state before the payload smoke, so a packaging defect shows
 * the actual installed engine packages instead of only the resolver's error.
 * @returns Nothing.
 */
function reportOfficeEngines(): void {
  try {
    const modules = join(paths.dsh, 'node_modules', '@deepseek-ai')
    const installed = readdirSync(modules).filter(name => name.startsWith('libreoffice-kit'))
    console.log(`desktop smoke diagnostics: prepared engines = ${JSON.stringify(installed)}`)
    for (const name of installed) {
      try {
        const manifest = JSON.parse(readFileSync(join(modules, name, 'package.json'), 'utf8')) as { version?: string }
        console.log(`desktop smoke diagnostics:   ${name}@${manifest.version ?? '?'}`)
      } catch {
        console.log(`desktop smoke diagnostics:   ${name}: package.json unreadable`)
      }
    }
    const kit = JSON.parse(readFileSync(join(modules, 'libreoffice-kit', 'package.json'), 'utf8')) as {
      version?: string
      optionalDependencies?: Record<string, string>
    }
    console.log(`desktop smoke diagnostics: kit ${kit.version ?? '?'} optional=${JSON.stringify(Object.keys(kit.optionalDependencies ?? {}))}`)
    try {
      const unpackedRoot = join(application, 'resources', 'app.asar.unpacked', 'dsh', 'node_modules', '@deepseek-ai')
      const unpacked = readdirSync(unpackedRoot).filter(name => name.startsWith('libreoffice-kit'))
      console.log(`desktop smoke diagnostics: unpacked engines = ${JSON.stringify(unpacked)}`)
      for (const name of unpacked) {
        try {
          readFileSync(join(unpackedRoot, name, 'package.json'))
          console.log(`desktop smoke diagnostics:   ${name}: package.json present`)
        } catch {
          console.log(`desktop smoke diagnostics:   ${name}: package.json MISSING`)
        }
      }
    } catch (error) {
      console.log(`desktop smoke diagnostics: unpacked listing failed: ${String(error)}`)
    }
  } catch (error) {
    console.log(`desktop smoke diagnostics: failed: ${String(error)}`)
  }
}

const descriptor = await verifyDesktopRuntime(paths.dsh, readDesktopRuntime(paths.dsh).release.version,
  resolveDesktopPackageTarget(target))
if (windows && !values.unsigned) await verifyWindowsCode(application)
reportOfficeEngines()
await smokePreparedRuntime(join(resources, 'app.asar', 'dsh'), executable, join(resources, 'runtime'), descriptor)
