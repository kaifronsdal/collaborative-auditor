// plotly.js-basic-dist-min ships no types; we only mount it on `window` so
// the kernel-emitted inline `Plotly.newPlot(...)` scripts resolve.
declare module "plotly.js-basic-dist-min" {
  const Plotly: unknown;
  export default Plotly;
}
