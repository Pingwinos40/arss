# uPlot (vendored, files pending)

`html/index.html` loads the chart library from this folder first and falls back
to unpkg only if the local files are missing (issue #33). The dashboard runs on
a bench network that may have no internet access, so the local copy is the
intended source.

Place these two files here, unmodified, from the uPlot 1.6.17 release:

| File in this folder   | Source path in the uPlot 1.6.17 package |
|-----------------------|------------------------------------------|
| `uPlot.iife.min.js`   | `dist/uPlot.iife.min.js`                 |
| `uPlot.min.css`       | `dist/uPlot.min.css`                     |

uPlot is MIT licensed (https://github.com/leeoniya/uPlot). Keep its `LICENSE`
file next to the two files when they are added.

Status: not downloaded yet. Adding them is pending the repo owner's approval.
Until then the page uses the unpkg fallback when the browser has internet
access, and shows "chart unavailable" (with the rest of the dashboard still
working) when it does not.
