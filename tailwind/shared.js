// Class names some pages build in JavaScript (e.g. `text-${tone}-400`): always included.
module.exports.safelist = [
  { pattern: /^(bg|text|border)-(red|green|emerald|rose|amber|yellow|sky|blue|indigo|slate|gray|purple|orange|teal|cyan|lime|pink|violet)-(300|400|500|600)$/ },
  { pattern: /^(bg|border)-(red|green|emerald|rose|amber|yellow|sky|blue|indigo|slate|gray|purple|orange)-(400|500|600)\/(5|10|15|20|25|30|40|50)$/ },
];
const all = ['../store/templates/**/*.html', '../store/static/store/js/**/*.js', '../store/**/*.py'];
module.exports.content = (extra) => all.concat(extra || []);
