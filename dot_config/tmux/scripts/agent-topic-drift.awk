# agent-topic-drift.awk — lexical topic-drift PRE-FILTER for agent tab titles.
#
# Claude Code names a conversation once (ai-title, end of the first turn) and
# essentially never again, so a session that wanders from "fix the parser" to
# "port the watcher to rust" keeps the parser title all day. This decides,
# per submitted prompt and with no model call, whether the conversation's
# goal MAY have moved. It is a cheap gate, not the verdict: lexical overlap
# also misses same-task follow-ups that share no words (proofing rounds,
# logistics), so when it fires agent-tab-indicator.sh asks the condenser to
# JUDGE — keep the current label or name the new task. See
# docs/agent-tab-indicator.md, "Renames and topic drift". Standalone so an
# offline calibration replay drives the exact code.
#
# Invocation (BWK awk compatible: macOS /usr/bin/awk, no gensub/asort/
# IGNORECASE/\<). RUN IT UNDER LC_ALL=C: macOS awk slices bytes with substr
# but decodes UTF-8 in tolower/match under a UTF-8 locale, and exits 2 on a
# cut that split a character. Cuts here also drop a trailing incomplete UTF-8
# sequence, so every text this writes stays valid UTF-8.
#   LC_ALL=C DRIFT_PROMPT=<prompt> DRIFT_TITLE=<current title, seeds a fresh
#   state> [DRIFT_CUSTOM=<latest custom-title>] [DRIFT_AI=<latest ai-title>]
#   [DRIFT_LAUNCH=<the transcript's first custom-title; used on a fresh state>]
#   awk -v now=<epoch> -v state_out=<tmpfile> [-v strikes_needed=3 ...]
#       -f agent-topic-drift.awk <state|/dev/null>
# Text comes through ENVIRON, never -v: -v processes backslash escapes.
# The full new state is ALWAYS written to state_out (the caller mv's it over
# the old file). stdout is one verdict line; it contains RETITLE exactly when
# a retitle fired (the hook keys on that to record retitle_prev).
#
# Model:
#   W     = unique content words of the prompt's first max_chars bytes, after
#           <pasted_content …>…</pasted_content> blocks are cut out (an
#           unterminated one runs to the end): lowercased, split on
#           non-alphanumerics, <3-char / numeric / stopword tokens dropped,
#           crude suffix stemming
#   bag   = union of the last `keep` on-topic word sets (a ring), seeded with
#           the title's words on first use and reseeded on a /rename
#   overlap = |W ∩ bag| / |W|
# Prompts that carry no goal are skipped outright (after a left trim):
# system/tag text ('<…'), slash commands ('/…'), peer agents ('[message from
# @…'), compaction continuations ('This session is being continued from a
# previous conversation…'), and anything with fewer than min_words content
# words ("yes", "do it", "lgtm" — most prompts).
# overlap >= drift_t: on topic — W joins the ring, strikes and candidates
# reset. Below: a strike, +1 more if the drifted prompt has a pivot phrase
# ("switching gears" & co.), and the prompt is kept as a candidate. Enough
# strikes, past the first substantive turn and outside the cooldown, fire a
# retitle: retitle_raw = the candidates joined, the ring restarts from their
# words.
#
# State file: line-oriented "key<TAB>value", first line "v<TAB>1". A missing
# header reads as a fresh state; unparseable lines are dropped.
#   turns strikes retitle_at          integers
#   launch_custom                     custom-title present when the state was
#                                     created (a launcher's, e.g. CuaNotch Tasks
#                                     slugs: DRIFT_LAUNCH, else DRIFT_CUSTOM) —
#                                     never changed; not a user rename
#   seen_custom                       the custom-title last seen (rename check)
#   seen_ai                           the ai-title last seen (the transcript's
#                                     title lines can fall out of the hook's
#                                     64KB tail; this is the fallback)
#   retitle_raw                       prompt-grade text for the condenser
#   retitle_custom                    custom-title current when retitle fired
#   retitle_prev                      label shown when retitle fired; appended
#                                     by the HOOK after a RETITLE verdict,
#                                     carried through here, cleared on the next
#   bag   <words>                     one per ring slot, oldest first
#   cand  <words><TAB><text>          drifted prompts since the last on-topic

function trim(s) {
    gsub(/^[ ]+/, "", s); gsub(/[ ]+$/, "", s)
    return s
}

# One line, no control characters, single spaces.
function clean(s) {
    gsub(/[\001-\037\177]/, " ", s)
    gsub(/  +/, " ", s)
    return trim(s)
}

# Drop a trailing INCOMPLETE UTF-8 sequence (what a byte cut leaves behind).
# Byte classes need LC_ALL=C.
function utf8_trim(s,    n, k, c, need) {
    n = length(s)
    k = 0
    while (k < 3 && k < n && substr(s, n - k, 1) ~ /[\200-\277]/) k++
    if (k >= n) return s
    c = substr(s, n - k, 1)
    if (c ~ /[\360-\367]/) need = 4
    else if (c ~ /[\340-\357]/) need = 3
    else if (c ~ /[\300-\337]/) need = 2
    else return (k > 0 && c !~ /[\200-\377]/) ? substr(s, 1, n - k) : s
    return (k + 1 < need) ? substr(s, 1, n - k - 1) : s
}

# First n bytes, backed off to a word boundary when one is in reach, never
# ending mid-character.
function cut(s, n,    t) {
    if (length(s) <= n) return s
    t = substr(s, 1, n)
    if (match(t, / [^ ]*$/) && RSTART > n / 2) t = substr(t, 1, RSTART - 1)
    return utf8_trim(t)
}

# Cut out <pasted_content …>…</pasted_content> blocks; an unterminated one
# runs to the end. A paste is material, not the user's goal.
function strip_pasted(s,    i, j, rest, out) {
    out = ""
    while ((i = index(s, "<pasted_content")) > 0) {
        out = out substr(s, 1, i - 1) " "
        rest = substr(s, i)
        j = index(rest, "</pasted_content>")
        if (j == 0) { s = ""; break }
        s = substr(rest, j + 17)
    }
    return out s
}

function stem(w,    n) {
    n = length(w)
    if (n - 3 >= 4 && substr(w, n - 2) == "ing") return substr(w, 1, n - 3)
    if (n - 2 >= 4 && substr(w, n - 1) == "ed") return substr(w, 1, n - 2)
    if (n - 2 >= 4 && substr(w, n - 1) == "es") return substr(w, 1, n - 2)
    if (n - 1 >= 4 && substr(w, n) == "s" && substr(w, n - 1, 1) != "s") return substr(w, 1, n - 1)
    return w
}

# Unique content words of s, space-joined (first-seen order); count in NW.
function words(s,    raw, n, i, t, st, seen, out) {
    NW = 0
    out = ""
    s = tolower(s)
    n = split(s, raw, /[^a-z0-9]+/)
    for (i = 1; i <= n; i++) {
        t = raw[i]
        if (length(t) < 3 || t ~ /^[0-9]+$/ || (t in STOP)) continue
        st = stem(t)
        if (length(st) < 3 || (st in STOP) || (st in seen)) continue
        seen[st] = 1
        out = out (NW ? " " : "") st
        NW++
    }
    return out
}

function ring_push(w) {
    if (w == "") return
    BAG[++NBAG] = w
    while (NBAG > keep) ring_shift()
}

function ring_shift(    i) {
    for (i = 1; i < NBAG; i++) BAG[i] = BAG[i + 1]
    delete BAG[NBAG]
    NBAG--
}

function ring_reset(w) {
    split("", BAG); NBAG = 0
    ring_push(w)
}

function cands_reset() {
    split("", CW); split("", CT); NC = 0
}

function cand_push(w, t,    i) {
    CW[++NC] = w; CT[NC] = t
    while (NC > 3) {
        for (i = 1; i < NC; i++) { CW[i] = CW[i + 1]; CT[i] = CT[i + 1] }
        delete CW[NC]; delete CT[NC]; NC--
    }
}

function int_or0(v) { return (v ~ /^[0-9]+$/) ? v + 0 : 0 }

function oneline(s) { gsub(/[\t\r\n]/, " ", s); return s }

BEGIN {
    FS = "\t"
    if (min_words == "") min_words = 4
    if (drift_t == "") drift_t = 0.15
    if (strikes_needed == "") strikes_needed = 3
    if (keep == "") keep = 6
    if (cooldown == "") cooldown = 1200
    if (min_turn == "") min_turn = 2
    if (max_chars == "") max_chars = 600
    min_words += 0; drift_t += 0; strikes_needed += 0; keep += 0
    cooldown += 0; min_turn += 0; max_chars += 0; now += 0
    if (keep < 1) keep = 1

    # Common English function words plus the casual filler prompts are made
    # of. Generous on purpose: a filler word left in matches across topics
    # (false on-topic) and pads unrelated prompts (false drift) alike.
    sw = "the a an and or but nor not no yes yeah yep yup nope ok okay oke " \
         "is are was were be been being am do does did done doing doesn don didn " \
         "have has had having will wont won shall may might must can cant could couldn " \
         "should shouldn would wouldn isn aren wasn weren haven hasn hadn " \
         "i me my mine myself we us our ours you your yours yourself he him his " \
         "she her hers it its itself they them their theirs this that these those " \
         "what which who whom whose when where why how whats thats theres heres " \
         "there here then than too very so such only own same other others " \
         "all any both each few more most some many much lot lots bit " \
         "of to in on at by for with from into onto out over under up down off " \
         "about above below again further once between through during before after " \
         "until while because since though although unless whether either neither " \
         "if else also just really like maybe still now even ever never always " \
         "already yet soon later again anyway anyways though tho " \
         "lowk lowkey idk lol lmao omg btw imo tbh fyi pls plz please thx thanks " \
         "thank ty gonna wanna gotta kinda sorta basically actually literally " \
         "probably definitely totally pretty quite rather fine sure cool nice good " \
         "great awesome perfect right well okay alright hmm hey hi hello " \
         "thing things stuff something anything everything nothing someone " \
         "way ways one ones two three first second next last new old " \
         "want wants need needs make makes made let lets get gets got getting " \
         "see sees saw seen look looks looking think thinks thought know knows " \
         "knew try tries tried trying use uses used using work works worked working " \
         "go goes going went come comes came put puts take takes took give gives " \
         "keep keeps tell said say says ask asked check checks run runs " \
         "can could should would able also back still sure yes " \
         "ill ive im id youre youve youll theyre weve dont doesnt didnt cant " \
         "isnt arent wasnt werent wont wouldnt shouldnt couldnt havent hasnt " \
         "help day today currently " \
         "add fix change update remove create implement write show set find " \
         "file files code wrong broken bug bugs issue issues problem problems " \
         "switch gear gears unrelated topic task another plan plans separate " \
         "question note " \
         "same different instead rest part whole kind sort etc via per " \
         "vs versus unable"
    n = split(sw, swa, / +/)
    for (i = 1; i <= n; i++) if (swa[i] != "") STOP[swa[i]] = 1

    PIVOTS = "switching gears|switch gears|unrelated|different question|different topic|new task|another thing|change of plans|separate question|on another note|new topic|something else|different thing"
    NPIV = split(PIVOTS, PIV, /\|/)

    ok = 0; bad = 0
    turns = 0; strikes = 0; retitle_at = 0
    launch_custom = ""; seen_custom = ""; seen_ai = ""
    retitle_raw = ""; retitle_custom = ""; retitle_prev = ""
    NBAG = 0; NC = 0
}

NR == 1 { if ($0 == "v\t1") ok = 1; else bad = 1; next }
bad { next }
{
    k = $1
    v = (length($0) > length(k)) ? substr($0, length(k) + 2) : ""
    if (k == "turns") turns = int_or0(v)
    else if (k == "strikes") strikes = int_or0(v)
    else if (k == "retitle_at") retitle_at = int_or0(v)
    else if (k == "launch_custom") launch_custom = v
    else if (k == "seen_custom") seen_custom = v
    else if (k == "seen_ai") seen_ai = v
    else if (k == "retitle_raw") retitle_raw = v
    else if (k == "retitle_custom") retitle_custom = v
    else if (k == "retitle_prev") retitle_prev = v
    else if (k == "bag") { if (v ~ /^[a-z0-9 ]+$/) BAG[++NBAG] = v }
    else if (k == "cand") {
        j = index(v, "\t")
        if (j > 1 && substr(v, 1, j - 1) ~ /^[a-z0-9 ]+$/ && NC < 3) {
            CW[++NC] = substr(v, 1, j - 1); CT[NC] = substr(v, j + 1)
        }
    }
}

END {
    fresh = !ok
    if (fresh) {
        turns = 0; strikes = 0; retitle_at = 0
        launch_custom = ""; seen_custom = ""; seen_ai = ""
        retitle_raw = ""; retitle_custom = ""; retitle_prev = ""
        split("", BAG); NBAG = 0; cands_reset()
    }
    while (NBAG > keep) ring_shift()

    custom = oneline(ENVIRON["DRIFT_CUSTOM"])
    ai = oneline(ENVIRON["DRIFT_AI"])
    if (ai != "") seen_ai = ai
    verdict = ""

    if (fresh) {
        ring_reset(words(ENVIRON["DRIFT_TITLE"]))
        # Whatever custom-title the session already had when we first saw it
        # was not a rename we watched happen (a launcher's slug, typically).
        # DRIFT_LAUNCH, when given, is the transcript's FIRST custom-title
        # (the hook reads the head once, at state creation): a long session's
        # slug is often outside the tail by then.
        launch = oneline(ENVIRON["DRIFT_LAUNCH"])
        launch_custom = (launch != "") ? launch : custom
        seen_custom = (custom != "") ? custom : launch_custom
    } else if (custom != "" && custom != seen_custom) {
        # The user renamed (/rename): anchor to their own naming from here.
        ring_reset(words(custom))
        strikes = 0; cands_reset()
        seen_custom = custom
        verdict = "renamed "
    }

    p = clean(strip_pasted(ENVIRON["DRIFT_PROMPT"]))
    sub(/^(\302\240| )+/, "", p)
    head = utf8_trim(substr(p, 1, max_chars))

    if (p == "" || substr(p, 1, 1) == "<" || substr(p, 1, 1) == "/" || index(p, "[message from @") == 1 \
        || index(p, "This session is being continued from a previous conversation") == 1) {
        verdict = verdict "skip tag"
    } else {
        W = words(head)
        nw = NW
        if (nw < min_words) {
            verdict = verdict "skip short " nw
        } else {
            turns++
            # Bag = union of the ring.
            split("", inbag)
            for (i = 1; i <= NBAG; i++) {
                m = split(BAG[i], bw, / /)
                for (j = 1; j <= m; j++) inbag[bw[j]] = 1
            }
            hit = 0
            m = split(W, ww, / /)
            for (j = 1; j <= m; j++) if (ww[j] in inbag) hit++
            ov = (NBAG > 0) ? hit / nw : 1
            if (ov >= drift_t) {
                ring_push(W)
                strikes = 0; cands_reset()
                verdict = verdict sprintf("on %.2f", ov)
            } else {
                strikes++
                # A pivot phrase is +1 on a prompt that ALREADY drifted; it
                # never fires by itself.
                low = tolower(head)
                for (i = 1; i <= NPIV; i++) if (index(low, PIV[i])) { strikes++; break }
                if (strikes > strikes_needed + 3) strikes = strikes_needed + 3
                cand_push(W, cut(p, 200))
                verdict = verdict sprintf("drift %.2f strikes=%d", ov, strikes)
                if (strikes >= strikes_needed && turns >= min_turn && now - retitle_at >= cooldown) {
                    raw = ""
                    for (i = 1; i <= NC; i++) raw = raw (i > 1 ? " / " : "") CT[i]
                    retitle_raw = cut(raw, 300)
                    retitle_at = now
                    # The custom-title in force now. An empty DRIFT_CUSTOM
                    # (its line fell out of the hook's tail) is not "none".
                    retitle_custom = (custom != "") ? custom : seen_custom
                    retitle_prev = ""   # the hook appends the shown label
                    split("", BAG); NBAG = 0
                    for (i = 1; i <= NC; i++) ring_push(CW[i])
                    strikes = 0; cands_reset()
                    verdict = verdict " RETITLE"
                }
            }
        }
    }

    if (state_out != "") {
        printf "v\t1\nturns\t%d\nstrikes\t%d\nretitle_at\t%d\n", turns, strikes, retitle_at > state_out
        if (launch_custom != "") printf "launch_custom\t%s\n", launch_custom > state_out
        if (seen_custom != "") printf "seen_custom\t%s\n", seen_custom > state_out
        if (seen_ai != "") printf "seen_ai\t%s\n", seen_ai > state_out
        if (retitle_raw != "") printf "retitle_raw\t%s\n", retitle_raw > state_out
        if (retitle_custom != "") printf "retitle_custom\t%s\n", retitle_custom > state_out
        if (retitle_prev != "") printf "retitle_prev\t%s\n", retitle_prev > state_out
        for (i = 1; i <= NBAG; i++) printf "bag\t%s\n", BAG[i] > state_out
        for (i = 1; i <= NC; i++) printf "cand\t%s\t%s\n", CW[i], CT[i] > state_out
        close(state_out)
    }
    print verdict
}
