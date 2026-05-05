# aigc - AI-Powered Git Commit Message Generator

A modern, intelligent tool that generates high-quality git commit messages using AI (Claude Haiku 4.5 via GitHub Copilot).

## Features

- **Smart Context Awareness**: Analyzes recent commits, branch names, and diff stats for consistent messaging
- **Conventional Commits**: Auto-detects and follows your repo's commit style
- **Beautiful UI**: Colorized output, animated spinners, and clean previews
- **Flexible Workflows**: Multiple modes for different use cases
- **Configurable**: Customize via config file or environment variables
- **Fast**: Uses Claude Haiku 4.5 for quick, quality responses
- **Validation**: Auto-validates commit messages against best practices

## Installation

The script is already executable. Just ensure you have:
- Python 3.7+
- git
- GitHub Copilot CLI (install with: `gh extension install github/gh-copilot`)

## Usage

### Basic Usage
```bash
aigc                    # Generate message and open editor
```

### Advanced Usage
```bash
aigc --dry-run          # Preview without committing
aigc --no-edit          # Commit directly (skip editor)
aigc --amend            # Amend last commit
aigc --all              # Stage all changes and commit
aigc --preview          # Show preview before editor
aigc --verbose          # Show detailed info
```

### Custom Model
```bash
aigc --model claude-sonnet-4.5
```

### Adding Context & Steering
You can provide additional context or steering instructions to influence the commit message:

```bash
# Using the --context flag
aigc --context "This fixes issue #123"

# Piping context from stdin
echo "Part of the authentication refactor" | aigc

# Reading context from a file
cat notes.txt | aigc

# Combining both methods
echo "Critical security fix" | aigc --context "Addresses CVE-2024-1234"

# Multi-line context
aigc --context "This is part of a larger refactoring.
Focus on the security improvements.
Mention this is work in progress."
```

Use cases for context:
- Reference issue/ticket numbers
- Explain broader context not visible in the diff
- Steer the tone or focus of the message
- Add information about why changes were made
- Mention related work or dependencies

## Configuration

Create `~/.config/aigc/config.json`:

```json
{
  "model": "claude-haiku-4.5",
  "max_history": 5,
  "max_diff_lines": 500,
  "timeout": 30,
  "style": "conventional",
  "include_stats": true,
  "include_history": true,
  "auto_validate": true,
  "show_preview": false
}
```

### Environment Variables

Override config with:
- `AIGC_MODEL` - AI model to use
- `AIGC_MAX_HISTORY` - Number of recent commits for context
- `AIGC_TIMEOUT` - Timeout in seconds
- `NO_COLOR` - Disable colors

## What's New in v2.0

### Migrated from Bash to Python
- Better error handling and user experience
- Cleaner code and easier to maintain
- Rich terminal UI with colors and animations

### Smart AI Prompting
- Includes recent commit history for style consistency
- Analyzes branch names for context (e.g., `fix/bug-123` → suggests `fix:`)
- Adds diff statistics to prompt
- Handles large diffs intelligently

### Flexible Workflows
- `--dry-run`: Preview without committing
- `--no-edit`: Trust the AI, commit directly
- `--amend`: Generate message for amending
- `--all`: Stage all changes automatically
- `--preview`: See message before editor opens

### Better Validation
- Checks subject line length
- Warns about formatting issues
- Validates conventional commit format
- Provides actionable suggestions

### Visual Enhancements
- Colorized output (respects NO_COLOR)
- Animated spinner with elapsed time
- Beautiful message preview boxes
- Clear error messages

## Examples

### Quick commit workflow
```bash
git add .
aigc --no-edit
```

### Preview before committing
```bash
git add src/feature.py
aigc --preview
```

### Amend with better message
```bash
aigc --amend
```

### Dry run to see what AI suggests
```bash
git add .
aigc --dry-run
```

## Tips

1. **Let the AI learn your style**: The more commits in your history, the better it matches your style
2. **Use branch names**: Name branches like `feat/user-auth` or `fix/login-bug` for better suggestions
3. **Review before committing**: Use `--preview` to see the message first
4. **Large changes**: The tool automatically summarizes diffs over 500 lines

## Backup

The original bash script is saved as `aigc.bak` in the same directory.

## Model Information

Default model: `claude-haiku-4.5`
- Fast responses (~2-3 seconds)
- High quality commit messages
- Cost-effective

Available models (via `copilot --help`):
- `claude-haiku-4.5` (recommended, fast)
- `claude-sonnet-4.5` (more detailed)
- `claude-sonnet-4` (balanced)
- `gpt-5`, `gpt-5.1` (OpenAI models)
- `gemini-3-pro-preview` (Google)

## Troubleshooting

### "copilot CLI not found"
Install GitHub Copilot CLI: `gh extension install github/gh-copilot`

### "Not inside a git repository"
Run `git init` or cd into a git repo

### "No staged changes"
Stage files first: `git add <files>` or use `aigc --all`

### Timeout errors
Increase timeout: `aigc --model claude-haiku-4.5` or set `AIGC_TIMEOUT=60`

## Version

Current version: 2.0.0

Check version: `aigc --version`
