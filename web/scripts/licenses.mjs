import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
const root = new URL('../', import.meta.url);
// The renderer and injected preload runtime ship in the browser bundle.
// Retain complete upstream notices, including their bundled attributions.
const sources = [
  ['Three.js', 'node_modules/three/LICENSE'],
  ['Vite', 'node_modules/vite/LICENSE.md'],
  ['Rolldown', 'node_modules/rolldown/LICENSE'],
  ['Rolldown third-party notices', 'node_modules/rolldown/THIRD-PARTY-LICENSE'],
];
const notices = await Promise.all(sources.map(async ([name, source]) => {
  const original = await readFile(new URL(source, root), 'utf8');
  const notice = original.replace(/\r\n/g, '\n').replace(/[ \t]+$/gm, '');
  return `${name}\n${'='.repeat(name.length)}\n\n${notice}`;
}));
await mkdir(new URL('public/', root), { recursive: true });
const destination = new URL('public/third-party-licenses.txt', root);
await writeFile(destination, notices.join('\n\n'), 'utf8');
console.log(`Included browser dependency notices in ${fileURLToPath(destination)}`);
