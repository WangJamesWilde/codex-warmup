import {build} from 'esbuild';
import {readFile, writeFile, mkdir} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const ui = path.resolve(here, '../codex-warmup/ui');
const compiled = await build({
  stdin: {contents: await readFile(path.join(ui, 'app.js'), 'utf8'), resolveDir: here, loader: 'js'},
  bundle: true, write: false, outfile: 'app.js', format: 'iife', minify: true,
  legalComments: 'inline', target: 'es2022', metafile: true,
});
const js = compiled.outputFiles.find(file => file.path.endsWith('.js')).text;
const css = compiled.outputFiles.find(file => file.path.endsWith('.css'))?.text || '';
const template = await readFile(path.join(ui, 'template.html'), 'utf8');
await mkdir(path.join(ui, 'dist'), {recursive: true});
const html = template
  .replace('/*__SDK_STYLES__*/', () => css)
  .replace('/*__APP_SCRIPT__*/', () => js.replaceAll('</script', '<\\/script'));
await writeFile(path.join(ui, 'dist/app.html'), html);
const licenses=[];
const seen=new Set();
for(const input of Object.keys(compiled.metafile.inputs)){
  const full=path.resolve(here,input).replaceAll('\\','/');
  const marker=full.lastIndexOf('/node_modules/');
  if(marker<0)continue;
  const rest=full.slice(marker+14).split('/');
  const count=rest[0].startsWith('@')?2:1;
  const root=full.slice(0,marker+14)+rest.slice(0,count).join('/');
  if(seen.has(root))continue;seen.add(root);
  const pkg=JSON.parse(await readFile(path.join(root,'package.json'),'utf8'));
  let license=null;
  for(const name of ['LICENSE','LICENSE.md','LICENSE.txt','license','LICENCE','LICENCE.md']){
    try{license=await readFile(path.join(root,name),'utf8');break;}catch{}
  }
  if(!license)throw Error('Missing license text for bundled package '+pkg.name);
  licenses.push(`${pkg.name} ${pkg.version} (${pkg.license})\n${license}`);
}
await writeFile(path.join(ui,'dist/THIRD_PARTY_LICENSES.txt'),licenses.join('\n\n--------------------\n\n'));
if(process.argv.includes('--qa')){
  const fixture=await readFile(path.join(here,'mock-host.html'),'utf8');
  const stateRoot=path.resolve(here,'../.state');await mkdir(stateRoot,{recursive:true});
  await writeFile(path.join(stateRoot,'ui-qa.html'),fixture.replace('/*__APP_JSON__*/',()=>JSON.stringify(html).replaceAll('</script','<\\/script')));
}
console.log('Built offline MCP App HTML; no frontend runtime or web server required.');
