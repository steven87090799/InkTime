# PhotoPainter firmware source notices

InkTime's `spectra6_73.cpp` uses board facts and E6 controller initialization
values from the `xiaozhi-esp32` subtree of
[Waveshare ESP32-S3-PhotoPainter](https://github.com/waveshareteam/ESP32-S3-PhotoPainter).
That subtree is distributed under the MIT License:

> MIT License
>
> Copyright (c) 2025 Shenzhen Xinzhi Future Technology Co., Ltd.<br>
> Copyright (c) 2025 Project Contributors
>
> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to deal
> in the Software without restriction, including without limitation the rights
> to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
> copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all
> copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
> AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
> LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
> OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
> SOFTWARE.

PhotoPainter pin/palette behavior was also cross-checked against
[PhotoPainter-Nginx-Home-Assistant-Device](https://github.com/will-rigby/PhotoPainter-Nginx-Home-Assistant-Device),
distributed under the BSD Zero Clause License:

> BSD Zero Clause License
>
> Copyright (c) 2026 PhotoPainter-Nginx-Home-Assistant-Device contributors
>
> Permission to use, copy, modify, and/or distribute this software for any
> purpose with or without fee is hereby granted.
>
> THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH
> REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY
> AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT,
> INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM
> LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR
> OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR
> PERFORMANCE OF THIS SOFTWARE.

The existing default-hardware path continues to use
[GxEPD2](https://github.com/ZinggJM/GxEPD2), licensed under GPL-3.0. See the
library repository for its complete license and distribution obligations.
