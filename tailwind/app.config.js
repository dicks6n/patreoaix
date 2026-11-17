// Same as the tailwind.config that base_dashboard.html gave the CDN.
const { safelist, content } = require('./shared');
module.exports = {
  darkMode: 'class',
  content: content(),
  safelist,
  theme: { extend: {
    colors: { darkBg: '#0b0d12', cardBg: '#12151c', cardBorder: '#232836', brandGreen: '#16c784' },
    fontFamily: { sans: ['Inter', 'sans-serif'], mono: ['JetBrains Mono', 'monospace'] },
  } },
};
