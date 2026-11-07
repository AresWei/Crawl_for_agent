// Local computation worker only. No network, upstream entrypoint or crawler.
// Algorithm attribution and restrictions are retained in douyin_sign.js.
const fs = require('node:fs');
const vm = require('node:vm');
const readline = require('node:readline');
const path = require('node:path');
const realm = vm.createContext({});
vm.runInContext(fs.readFileSync(path.join(__dirname, 'douyin_sign.js'), 'utf8'), realm);
readline.createInterface({input: process.stdin, terminal: false}).on('line', line => {
  try {
    const request = JSON.parse(line);
    if (!['sign_datail', 'sign_reply'].includes(request.fn)) throw new Error();
    realm.input = request;
    const signature = vm.runInContext('globalThis[input.fn](input.query, input.ua)', realm, {timeout: 5000});
    delete realm.input;
    process.stdout.write(JSON.stringify({signature}) + '\n');
  } catch (_) {
    process.stdout.write('{"error":"sign_failed"}\n');
  }
});
