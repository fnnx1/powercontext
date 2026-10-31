# 初始化数据

无需预置数据。companion 启动时自动创建：

- `story_state(session_id, turn, last_action, updated_at)`
- `story_beats(id, session_id, turn, action, story, created_at)`

建议用两个会话验证隔离：`demo` 与 `alice`。清空演示状态：

```sql
DELETE FROM story_beats WHERE session_id IN ('demo','alice');
DELETE FROM story_state WHERE session_id IN ('demo','alice');
```
