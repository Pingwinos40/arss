# uPlot (vendored)

`html/index.html` loads the chart library from this folder first and falls back
to unpkg only if the local files are missing (issue #33). The dashboard runs on
a bench network that may have no internet access, so the local copy is the
intended source.

These files are unmodified copies from the uPlot 1.6.17 npm package:

| File in this folder   | Source path in the package | sha384 (SRI)                                                              |
|-----------------------|----------------------------|---------------------------------------------------------------------------|
| `uPlot.iife.min.js`   | `dist/uPlot.iife.min.js`   | `ZQMJolSBnNYZvYl4RIsowWxyMp4FOzy4QOlSDeXMCWJ8CxMSY6rK1FSGCEXJwvd1`        |
| `uPlot.min.css`       | `dist/uPlot.min.css`       | `4MQaEqb46em5Tirg3on4yaYQ5ORcm1d+YcDWrD2pvuxCVbNm2R+QKKLRCTZKpuJj`        |
| `LICENSE`             | `LICENSE`                  |                                                                           |

Provenance: downloaded 2026-10-14 from
`https://registry.npmjs.org/uplot/-/uplot-1.6.17.tgz`; the tarball's sha512
matched the registry's published integrity value
(`sha512-WHNHvDCXURn+Qwb3QUUzP6rOxx+3kUZUspREyhkqmXCxFIND99l5z9intTh+uPEt+/EEu7lCaMjSd1uTfuTXfg==`).
The unpkg fallback in `index.html` carries the sha384 values above as SRI, so it
can only load these exact bytes.

uPlot is MIT licensed (https://github.com/leeoniya/uPlot); keep `LICENSE` next
to the files. `.gitattributes` marks this folder `-text` so git never rewrites
line endings and the bytes keep matching the hashes.
