// ==UserScript==
// @name         TI-Nspire Connect — Chrome spoof (Arc fix)
// @namespace    local.ti.nspire.fix
// @version      1.0
// @description  Make nspireconnect.ti.com believe Arc is current Google Chrome by spoofing UA + Client Hints at document-start.
// @match        https://nspireconnect.ti.com/*
// @run-at       document-start
// @grant        none
// @noframes     false
// ==/UserScript==

(function () {
  'use strict';

  // ---- Tweak this if TI ever requires a newer version --------------------
  const CHROME_MAJOR = '140';
  const CHROME_FULL  = '140.0.0.0';
  // macOS UA string. Change the platform block if you're on Windows/Linux.
  const UA = `Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${CHROME_FULL} Safari/537.36`;
  // ------------------------------------------------------------------------

  const define = (obj, prop, value) => {
    try {
      Object.defineProperty(obj, prop, { get: () => value, configurable: true });
    } catch (e) { /* ignore locked props */ }
  };

  // 1) Classic UA surface
  define(navigator, 'userAgent', UA);
  define(navigator, 'appVersion', UA.replace('Mozilla/', ''));
  define(navigator, 'vendor', 'Google Inc.');

  // 2) Modern Client Hints (navigator.userAgentData)
  const brands = [
    { brand: 'Chromium', version: CHROME_MAJOR },
    { brand: 'Google Chrome', version: CHROME_MAJOR },
    { brand: 'Not=A?Brand', version: '24' },
  ];

  const highEntropy = {
    architecture: 'arm',
    bitness: '64',
    brands: brands,
    fullVersionList: [
      { brand: 'Chromium', version: CHROME_FULL },
      { brand: 'Google Chrome', version: CHROME_FULL },
      { brand: 'Not=A?Brand', version: '24.0.0.0' },
    ],
    mobile: false,
    model: '',
    platform: 'macOS',
    platformVersion: '15.0.0',
    uaFullVersion: CHROME_FULL,
    wow64: false,
  };

  const fakeUAData = {
    brands: brands,
    mobile: false,
    platform: 'macOS',
    getHighEntropyValues: (hints) => {
      const out = {};
      (hints || []).forEach((h) => { if (h in highEntropy) out[h] = highEntropy[h]; });
      // always include the low-entropy basics
      out.brands = brands;
      out.mobile = false;
      out.platform = 'macOS';
      return Promise.resolve(out);
    },
    toJSON: () => ({ brands: brands, mobile: false, platform: 'macOS' }),
  };

  define(navigator, 'userAgentData', fakeUAData);
})();
