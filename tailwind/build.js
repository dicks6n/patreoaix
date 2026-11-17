// Builds the site's CSS once (instead of the Tailwind CDN compiling it in every visitor's browser).
//   cd tailwind && npm install && npm run build      → store/static/store/css/tw-app.css, tw-landing.css, tw-auth.css
// Run it again after adding new Tailwind classes to a template, then commit the CSS files.
const { execFileSync } = require('child_process');
const path = require('path');
const bin = path.join(__dirname, 'node_modules', '.bin', 'tailwindcss');
for (const name of ['app', 'landing', 'auth']) {
  const out = path.join(__dirname, '..', 'store', 'static', 'store', 'css', `tw-${name}.css`);
  execFileSync(bin, ['-c', path.join(__dirname, `${name}.config.js`), '-i', path.join(__dirname, 'input.css'), '-o', out, '--minify'], { stdio: 'inherit' });
}
