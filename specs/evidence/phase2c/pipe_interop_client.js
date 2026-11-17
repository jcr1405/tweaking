/**
 * Presence Phase 2C - Experiment A: Node.js libuv Named Pipe Client
 * Connects to \\.\pipe\presence-production-interop-test
 */

const net = require('net');

const PIPE_PATH = '\\\\.\\pipe\\presence-production-interop-test';
console.log(`CLIENT: Node.js ${process.version}, libuv ${process.versions.uv}`);
console.log(`CLIENT: Connecting to ${PIPE_PATH}...`);

const client = net.connect(PIPE_PATH, () => {
    console.log('CLIENT: Connected via libuv!');
    const msg = 'Hello from Node.js libuv client!';
    console.log(`CLIENT: Sending ${msg.length} bytes: '${msg}'`);
    client.write(msg);
});

client.on('data', (data) => {
    console.log(`CLIENT: Received ${data.length} bytes: '${data.toString()}'`);
    console.log('CLIENT: Closing socket cleanly.');
    client.end();
});

client.on('end', () => {
    console.log('CLIENT: Server disconnected (EOF received).');
});

client.on('close', () => {
    console.log('CLIENT: Socket closed.');
    process.exit(0);
});

client.on('error', (err) => {
    console.error(`CLIENT ERROR: ${err.message}`);
    process.exit(1);
});
