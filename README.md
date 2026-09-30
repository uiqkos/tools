# tools

Сборник полезных инструментов.

## claude_usage.py — лимиты Claude Code и бюджеты по проектам

Показывает, сколько израсходовано из лимитов подписки Claude (5-часовое и недельное окно),
какую долю недели съел каждый проект, и позволяет задать проекту бюджет вида
«не больше 10% недельного лимита». Одна зависимость — Python 3.9+ (только стандартная библиотека).

```
$ claude-usage
Проект: ~/Projects/my-app
 5-часовой: ~27.0% использовано, сброс Wed 30.09 23:10 (через 1ч 12м), за час +6.4%
 Недельный: ~83.0% использовано, сброс Fri 02.10 09:00 (через 1д 10ч 32м)
            этот проект в текущем окне: ~4.6%

Этот проект (доля недельного лимита, оценка):
    1h:    0.41%  $    3.10 экв.     42 ответов  1 сессий
    5h:    1.20%  $    9.05 экв.    130 ответов  3 сессий
   24h:    2.90%  $   21.80 экв.    310 ответов  6 сессий
    7d:    4.56%  $   34.30 экв.    520 ответов  11 сессий

Проекты в текущем недельном окне:
    41.2%  $  310.00  ~/Projects/other
     4.6%  $   34.30  ~/Projects/my-app
```

### Как считает

- **Реальные проценты лимитов** Claude Code передаёт в statusline (поле `rate_limits`, только на
  подписках Pro/Max). Statusline дописывает их в `~/.claude/usage-monitor/snapshots.jsonl`
  (см. «Установка»).
- **Доля проекта** — оценка: скрипт читает транскрипты сессий `~/.claude/projects/**/*.jsonl`,
  переводит токены каждого ответа в «доллары по ценам API» (с учётом модели, кэша и fast mode) и
  делит недельный процент пропорционально: `стоимость_проекта / стоимость_всего × недельный_%`.
- Между снимками лимитов проценты экстраполируются по токенам (такие значения помечены `~`).
- Проект = корень git-репозитория; git worktree (в т.ч. `.claude/worktrees/*`) считаются тем же проектом.
- Расход вне Claude Code на этой машине (claude.ai, другие компьютеры) не виден, поэтому доля
  проектов скорее завышена, чем занижена.
- Разбор транскриптов инкрементальный, кэш — `~/.claude/usage-monitor/transcripts-cache.json`.

### Установка

1. Положить скрипт и сделать команду:

   ```bash
   mkdir -p ~/.claude/usage-monitor ~/.local/bin
   curl -o ~/.claude/usage-monitor/claude_usage.py \
     https://raw.githubusercontent.com/uiqkos/tools/main/claude_usage.py
   chmod +x ~/.claude/usage-monitor/claude_usage.py
   ln -s ~/.claude/usage-monitor/claude_usage.py ~/.local/bin/claude-usage
   ```

2. Писать снимки лимитов из statusline. Добавить в свой скрипт statusline (нужен `jq`),
   сразу после чтения stdin в `input`:

   ```bash
   input=$(cat)

   # Снимки лимитов для claude_usage.py: пишем, только если проценты изменились или прошло 10 минут
   rl=$(echo "$input" | jq -c '.rate_limits // empty | {f:(.five_hour.used_percentage // null), fr:(.five_hour.resets_at // null), s:(.seven_day.used_percentage // null), sr:(.seven_day.resets_at // null)}' 2>/dev/null)
   if [ -n "$rl" ]; then
     um_dir="$HOME/.claude/usage-monitor"
     mkdir -p "$um_dir"
     now=$(date +%s)
     key="$(echo "$rl" | jq -r '"\(.f)|\(.s)|\(.fr)|\(.sr)"')"
     last_key="" last_ts=0
     [ -f "$um_dir/last-snapshot" ] && read -r last_ts last_key < "$um_dir/last-snapshot"
     if [ "$key" != "$last_key" ] || [ $((now - last_ts)) -ge 600 ]; then
       echo "$input" | jq -c --argjson ts "$now" '{ts:$ts, session_id, cwd:.workspace.current_dir,
         five_hour_pct:.rate_limits.five_hour.used_percentage, five_hour_resets_at:.rate_limits.five_hour.resets_at,
         seven_day_pct:.rate_limits.seven_day.used_percentage, seven_day_resets_at:.rate_limits.seven_day.resets_at}' >> "$um_dir/snapshots.jsonl"
       echo "$now $key" > "$um_dir/last-snapshot"
     fi
   fi
   ```

   Первые цифры появятся после первого ответа модели.

3. (По желанию) хук, который подсказывает агенту остаток бюджета проекта перед каждым
   промптом — в `~/.claude/settings.json`:

   ```json
   {
     "hooks": {
       "UserPromptSubmit": [
         { "hooks": [ { "type": "command",
                        "command": "python3 ~/.claude/usage-monitor/claude_usage.py hook",
                        "timeout": 15 } ] }
       ]
     }
   }
   ```

   Хук молчит в проектах без бюджета. Если бюджет задан, он добавляет в контекст строку
   с расходом, а при превышении или остатке меньше 20% — просьбу экономить.

### Команды

| Команда | Что делает |
|---|---|
| `claude-usage` / `claude-usage report` | полный отчёт по текущему проекту (cwd → корень git) |
| `claude-usage report --since 2h` | ещё и расход с момента: `30m`, `2h`, `1d`, `1w` или ISO-время |
| `claude-usage report --json` | то же в JSON (для скриптов и агентов) |
| `claude-usage report --top 15` | сколько проектов показывать в топе (по умолчанию 8) |
| `claude-usage check` | одна строка статуса; exit `0` — в бюджете, `2` — превышен, `3` — нет бюджета или данных |
| `claude-usage budget set 10` | бюджет проекта — 10% недельного лимита |
| `claude-usage budget clear` | снять бюджет проекта |
| `claude-usage budget list` | все бюджеты |
| `claude-usage hook` | точка входа для хука `UserPromptSubmit` |
| `-p, --project DIR` | другой проект вместо текущей директории |

### Цены моделей

Таблица `PRICES` в начале скрипта — цены API за 1M токенов (input, output, cache read); запись
в кэш считается как 1.25× input (TTL 5 минут) или 2× input (1 час). Важны только соотношения
между моделями: скрипт исходит из того, что лимиты подписки масштабируются как цены API.
Для новой модели добавьте строку с её префиксом. Неизвестные модели считаются по `DEFAULT_PRICE`.
