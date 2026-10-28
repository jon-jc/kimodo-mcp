import { cp, mkdir, realpath, rm, stat } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
const root = await realpath(fileURLToPath(new URL('../../', import.meta.url)));
const source = path.join(root, 'web', 'dist');
const target = path.resolve(root, 'src', 'kimodo_mcp', 'studio_web');
const packageRoot = await realpath(path.join(root, 'src', 'kimodo_mcp'));
if (path.dirname(target) !== packageRoot || path.basename(target) !== 'studio_web') throw new Error('Unexpected studio package destination');
await stat(path.join(source, 'index.html'));
await mkdir(target, { recursive: true });
if (await realpath(target) !== target) throw new Error('Studio package destination cannot be a symlink or redirected folder');
// This exact generated directory is inside the verified package root. Remove
// stale hashed bundles so a wheel contains only the current build's assets.
await rm(target, { recursive: true, force: true });
await mkdir(target);
await cp(source, target, { recursive: true, force: true, errorOnExist: false });
console.log('Packaged the studio inside kimodo_mcp/studio_web');
