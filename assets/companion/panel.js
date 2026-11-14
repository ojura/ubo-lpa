// The side panel shows uBO's logger in a frame. The frame gets its address
// once uBO's background page accepts connections from uBO's pages
// (uboStarted() in ubo.js). Until then, the panel shows the notice that says
// why the logger is not there.
const state = document.getElementById('state');
const frame = document.getElementById('ubo');

uboStarted(text => {
  state.textContent = text;
  state.hidden = text === '';
}).then(() => {
  frame.src = `${UBO_ORIGIN}/logger-ui.html`;
  frame.hidden = false;
});
