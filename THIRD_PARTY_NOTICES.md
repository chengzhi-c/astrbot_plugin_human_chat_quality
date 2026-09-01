# Third-Party Notices

This project includes portions of the Natural Talk system-prompt rules:

- Source: https://github.com/chengzhi-c/natural-talk
- Compared against: commit e137d229d3bcb6340d65d47dd07024c8fc630fc8, using SKILL.md, templates/system-prompt-lite.txt, and scripts/scan-mechanical.py
- License: MIT
- The lite prompt is included via build_stable_rules() with plugin extras (dialogue/sentence checks + action compact + iron rule + anti-checklist protections) — see quality_rules.py
- Selected high-confidence signals are adapted from upstream scan-mechanical.py / rules definitions (B4a, B10, D1, D4, C5, F7, identity/courtesy/signposts)

MIT License

Copyright (c) 2026 Natural Talk Contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
