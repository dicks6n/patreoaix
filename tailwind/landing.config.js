// Same as the tailwind.config that landing.html gave the CDN.
const { safelist, content } = require('./shared');
module.exports = { content: content(), safelist, theme: { extend: { colors: { darkBg: '#090d16', cardBg: '#0d1322' } } } };
