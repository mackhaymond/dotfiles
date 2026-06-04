// Briefly flash a colored border around a display, then fade out.
// Invoked as: osascript -l JavaScript yabai_screen_flash.js <displayID> <border>
//             <r> <g> <b> <hold> <fade> <radius> [<w> <h>]
//
// <displayID> is yabai's display .id (the CGDirectDisplayID). We locate the
// matching NSScreen by NSScreenNumber, falling back to a point-size match.
// Colors are 0..1 sRGB. The overlay is borderless, click-through, non-activating,
// and shows on the active space of the target screen.

function run(argv) {
  ObjC.import('Cocoa');
  ObjC.import('CoreGraphics');

  var did    = parseInt(argv[0], 10);
  var border = parseFloat(argv[1]);
  var r = parseFloat(argv[2]), g = parseFloat(argv[3]), b = parseFloat(argv[4]);
  var hold   = parseFloat(argv[5]);
  var fade   = parseFloat(argv[6]);
  var radius = parseFloat(argv[7]);
  var W = argv.length > 8 ? parseFloat(argv[8]) : -1;
  var H = argv.length > 9 ? parseFloat(argv[9]) : -1;

  var screens = $.NSScreen.screens;
  var target = null;

  // Primary match: CGDirectDisplayID == NSScreenNumber.
  for (var i = 0; i < screens.count; i++) {
    var s = screens.objectAtIndex(i);
    if (s.deviceDescription.objectForKey('NSScreenNumber').intValue === did) {
      target = s;
      break;
    }
  }
  // Fallback match: by point size (handles any id/NSScreenNumber mismatch).
  if (target === null && W > 0 && H > 0) {
    for (var j = 0; j < screens.count; j++) {
      var s2 = screens.objectAtIndex(j);
      var fr2 = s2.frame;
      if (Math.abs(fr2.size.width - W) < 2 && Math.abs(fr2.size.height - H) < 2) {
        target = s2;
        break;
      }
    }
  }
  if (target === null) return 'no-match';

  var f = target.frame; // AppKit coords (bottom-left origin), already per-screen
  var inset = border / 2 + 1;
  var rect = $.NSMakeRect(
    f.origin.x + inset, f.origin.y + inset,
    f.size.width - 2 * inset, f.size.height - 2 * inset
  );

  $.NSApplication.sharedApplication;
  // styleMask 0 = borderless; backing 2 = buffered.
  var w = $.NSWindow.alloc.initWithContentRectStyleMaskBackingDefer(rect, 0, 2, false);
  w.setOpaque(false);
  w.setBackgroundColor($.NSColor.clearColor);
  w.setLevel(1000); // screen-saver level: above ordinary windows
  w.setIgnoresMouseEvents(true);
  w.setHasShadow(false);
  // CanJoinAllSpaces (1) | Stationary (16): show on the active space, no animation.
  w.setCollectionBehavior((1 << 0) | (1 << 4));

  var v = $.NSView.alloc.initWithFrame($.NSMakeRect(0, 0, rect.size.width, rect.size.height));
  v.setWantsLayer(true);
  v.layer.setBorderWidth(border);
  // Build the CGColor directly via CoreGraphics. Converting a dynamically
  // created NSColor to .CGColor through the JXA bridge crashes (SIGKILL); the
  // direct CG call is what the layer wants anyway.
  v.layer.setBorderColor($.CGColorCreateGenericRGB(r, g, b, 1.0));
  v.layer.setCornerRadius(radius);
  w.setContentView(v);
  w.orderFrontRegardless;

  var rl = $.NSRunLoop.currentRunLoop;
  rl.runUntilDate($.NSDate.dateWithTimeIntervalSinceNow(hold));

  // Fade out smoothly, then remove.
  var steps = 12;
  for (var k = steps; k >= 0; k--) {
    w.setAlphaValue(k / steps);
    rl.runUntilDate($.NSDate.dateWithTimeIntervalSinceNow(fade / steps));
  }
  w.orderOut(w);
  return 'flashed';
}
