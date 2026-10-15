/*
 * tmux-shim.c: ~/.local/bin/tmux, a PATH shim in front of the real tmux.
 * Docs: ~/.config/tmux/docs/kill-guard.md
 *
 * Fast path (every ordinary call): no argument contains "kill-se" -> execv the
 * real tmux at once. No allocation, no environment lookups, one strstr per
 * argument. "kill-se" covers kill-session and kill-server and every unique
 * prefix of either (kill-ses.., kill-ser..); "kill-se" itself is ambiguous and
 * tmux rejects it.
 *
 * Slow path: exec /bin/bash KILL_GUARD cli ARGS..., which decides whether the
 * call would kill something protected on the real default server (Touch ID
 * first) or just execs the real tmux unchanged.
 *
 * Never recurses: both this file and the guard name the real tmux by absolute
 * path. Fails OPEN: if the guard is missing or bash can't start, warn on stderr
 * and run the real tmux, because a broken guard must not break every tmux call
 * (status line, scripts, agents). The server-side kill-server alias still
 * guards the full word in that case.
 *
 * Build (see .chezmoiscripts/run_onchange_after_build-tmux-kill-guard.sh.tmpl):
 *   clang -O2 -DREAL_TMUX='"/opt/homebrew/bin/tmux"' \
 *         -DKILL_GUARD='"/Users/<u>/.config/tmux/scripts/kill-guard.sh"' \
 *         -o tmux tmux-shim.c
 */
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#ifndef REAL_TMUX
#define REAL_TMUX "/opt/homebrew/bin/tmux"
#endif
#ifndef KILL_GUARD
#error "build with -DKILL_GUARD='\"/abs/path/kill-guard.sh\"'"
#endif
#ifndef GUARD_SHELL
#define GUARD_SHELL "/bin/bash"
#endif

static void
real_tmux(char **argv)
{
	execv(REAL_TMUX, argv);
	fprintf(stderr, "tmux shim: cannot exec %s: %s\n", REAL_TMUX,
	    strerror(errno));
	exit(127);
}

int
main(int argc, char **argv)
{
	char **nargv;
	int i;

	for (i = 1; i < argc; i++) {
		if (strstr(argv[i], "kill-se") != NULL)
			break;
	}
	if (i >= argc)		/* also argc == 0 */
		real_tmux(argv);

	if (access(KILL_GUARD, R_OK) != 0) {
		fprintf(stderr, "tmux shim: kill guard %s missing (%s); running "
		    "tmux UNGUARDED\n", KILL_GUARD, strerror(errno));
		real_tmux(argv);
	}
	nargv = malloc(((size_t)argc + 3) * sizeof *nargv);
	if (nargv == NULL)
		real_tmux(argv);
	nargv[0] = "bash";
	nargv[1] = KILL_GUARD;
	nargv[2] = "cli";
	for (i = 1; i < argc; i++)
		nargv[i + 2] = argv[i];
	nargv[argc + 2] = NULL;
	execv(GUARD_SHELL, nargv);
	fprintf(stderr, "tmux shim: cannot run kill guard via %s (%s); running "
	    "tmux UNGUARDED\n", GUARD_SHELL, strerror(errno));
	real_tmux(argv);
	return 127;
}
