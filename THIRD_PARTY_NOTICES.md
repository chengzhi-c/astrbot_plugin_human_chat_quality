# Third-Party Notices

This project includes portions of the Natural Talk system-prompt rules:

- Source: https://github.com/chengzhi-c/natural-talk
- Compared against: commit 4b0376021c3bf1f9727dfc4efa83cae8eb45acf0, using templates/system-prompt-lite.txt and core/rules.yaml extensions.iron_rule/action_compact
- License: MIT
- The lite prompt is included via build_stable_rules() with plugin extras (iron rule + action compact) — see quality_rules.py
- Selected high-confidence entries are adapted from dist/lexicon.json (tier1_identity/courtesy/tier2/signposts)

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
