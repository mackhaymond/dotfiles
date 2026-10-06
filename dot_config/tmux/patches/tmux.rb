class Tmux < Formula
  # LOCAL: backport of tmux master 57a13664 + 5e5e15f6 (cursor must not escape
  # synchronized updates). Drop this tap once a tmux release includes them.
  desc "Terminal multiplexer"
  homepage "https://tmux.github.io/"
  url "https://github.com/tmux/tmux/releases/download/3.7c/tmux-3.7c.tar.gz"
  sha256 "7c60cae9a0e25288e2e24750aafc9e8800fc7fd4555e447e1b29ee4201cfb3bf"
  license "ISC"
  revision 1

  patch :DATA
  compatibility_version 1

  livecheck do
    url :stable
    regex(/v?(\d+(?:\.\d+)+[a-z]?)/i)
    strategy :github_latest
  end


  head do
    url "https://github.com/tmux/tmux.git", branch: "master"

    depends_on "autoconf" => :build
    depends_on "automake" => :build
    depends_on "libtool" => :build
  end

  depends_on "pkgconf" => :build
  depends_on "libevent"
  depends_on "ncurses"
  depends_on "utf8proc"

  uses_from_macos "bison" => :build # for yacc

  on_macos do
    # https://github.com/tmux/tmux/blob/62044f02dff22d304da78ac81b69afcf84872ac7/CHANGES#L169-L170
    # https://github.com/tmux/tmux/issues/5385
    depends_on "jemalloc"
  end

  # runs a server as a test
  allow_network_access! :test

  def install
    system "sh", "autogen.sh" if build.head?

    args = %W[
      --enable-sixel
      --sysconfdir=#{etc}
      --enable-utf8proc
    ]

    # tmux finds the `tmux-256color` terminfo provided by our ncurses
    # and uses that as the default `TERM`, but this causes issues for
    # tools that link with the very old ncurses provided by macOS.
    # https://github.com/Homebrew/homebrew-core/issues/102748
    args << "--with-TERM=screen-256color" if OS.mac? && MacOS.version < :sonoma

    system "./configure", *args, *std_configure_args
    system "make", "install"

    pkgshare.install "example_tmux.conf"
  end

  def caveats
    <<~EOS
      Example configuration has been installed to:
        #{opt_pkgshare}
    EOS
  end

  test do
    system bin/"tmux", "-V"

    require "pty"

    socket = testpath/tap.user
    PTY.spawn bin/"tmux", "-S", socket, "-f", File::NULL
    sleep 10

    assert_path_exists socket
    assert_predicate socket, :socket?
    assert_equal "no server running on #{socket}", shell_output("#{bin}/tmux -S#{socket} list-sessions 2>&1", 1).chomp
  end
end

__END__
--- a/server-client.c	2026-10-06 07:37:06
+++ b/server-client.c	2026-10-06 07:37:06
@@ -1763,7 +1763,7 @@
 	struct window_pane	*wp = server_client_get_pane(c), *loop;
 	struct screen		*s = NULL;
 	struct options		*oo = c->session->options;
-	int			 mode = 0, cursor, flags;
+	int			 mode = 0, cursor, flags, pane_sync = 0;
 	u_int			 cx = 0, cy = 0, ox, oy, sx, sy, n;
 	struct visible_ranges	*r;
 
@@ -1808,6 +1808,11 @@
 		cx = c->prompt_cursor;
 	} else if (wp != NULL && c->overlay_draw == NULL) {
 		cursor = 0;
+		/*
+		 * Use the mode screen (s), not base: in copy mode the base may
+		 * be mid-sync while the cursor shown is the copy-mode one.
+		 */
+		pane_sync = (s->mode & MODE_SYNC);
 		tty_window_offset(tty, &ox, &oy, &sx, &sy);
 		if (wp->xoff + (int)s->cx >= (int)ox &&
 		    wp->xoff + (int)s->cx <= (int)ox + (int)sx &&
@@ -1831,8 +1836,20 @@
 	} else if (c->overlay_mode == NULL || s == NULL)
 		mode &= ~MODE_CURSOR;
 
-	log_debug("%s: cursor to %u,%u", __func__, cx, cy);
-	tty_cursor(tty, cx, cy);
+	/*
+	 * While the pane is in a synchronized update its contents are frozen,
+	 * so do not let the cursor position or on/off state escape either:
+	 * leave the cursor where the last full redraw put it (backport of
+	 * upstream 57a13664 and 5e5e15f6).
+	 */
+	if (!pane_sync) {
+		log_debug("%s: cursor to %u,%u", __func__, cx, cy);
+		tty_cursor(tty, cx, cy);
+	} else {
+		mode &= ~CURSOR_MODES;
+		mode |= tty->mode & CURSOR_MODES;
+		s = NULL;
+	}
 
 	/*
 	 * Set mouse mode if requested. To support dragging, always use button
