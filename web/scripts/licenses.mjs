import { copyFile, mkdir } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
const root = new URL('../', import.meta.url);
await mkdir(new URL('public/', root), { recursive: true });
await copyFile(new URL('node_modules/three/LICENSE', root), new URL('public/third-party-licenses.txt', root));
console.log(`Included Three.js MIT license in ${fileURLToPath(new URL('public/third-party-licenses.txt', root))}`);
