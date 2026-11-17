// Login / signup / OTP pages: the CDN's default config.
const { safelist, content } = require('./shared');
module.exports = { content: content(), safelist };
