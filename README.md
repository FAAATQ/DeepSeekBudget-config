# API Budget — 价格配置

[API Budget](https://github.com/FAAATQ/deepseek-budget) 的**实时价格与峰谷时段数据**。

app 里已经内置了一份同样的数据（编译进二进制，所以离线可用）。这个仓库是它的**可更新副本**：
官方改了价格或时段，改这里的 `deepseek.json` 并提交，用户点一下「检查更新」就能拿到——
**不必等 app 发新版本**。

## 文件

| 文件 | 内容 |
|---|---|
| [`deepseek.json`](deepseek.json) | DeepSeek 的价格与峰谷时段定义 |

app 从这个地址读取（**写死在代码里**，改它要同时改 app 的 CSP `connect-src`）：

```
https://raw.githubusercontent.com/FAAATQ/deepseek-budget-config/main/deepseek.json
```

## 怎么改价格

1. 编辑 `deepseek.json`
2. **把 `verifiedAt` 改成你核对官方页面的那一天**（`YYYY-MM-DD`）
3. 提交

`verifiedAt` 不只是个标注，它参与判断：**比用户手上那份旧的配置会被拒收**，
所以忘了改日期，更新就不会生效（这是有意的——防止 CDN 缓存把价格回滚）。

## app 会在应用前检查什么

app 收到这份 JSON 后，**任何一项不过就整个拒收**，用户当前的数据原样保留、托盘图标不受影响：

| 检查 | 挡的是什么 |
|---|---|
| 结构能反序列化 | 少字段、类型写错 |
| `provider` 仍为 `deepseek` | 一份配置把 app 悄悄换成别的 provider |
| `schedule` 能编译 | 时间格式写错、零长度窗口 |
| `verifiedAt` 是合法日期且**不比现有的旧** | 缓存回滚 |
| 每个价格有限、非负 | `null` / 负数 |
| 每个价格相对**用户当前生效那份**的偏离 ≤ 20 倍 | 小数点打错 |

> ⚠️ 最后一条是**限速器，不是安全边界**。它挡的是手滑，不是攻击——
> 能往这个仓库提交的人，就能改价格。真正的信任来源是 HTTPS + 这个仓库的写权限。

比较基准是**用户当前生效的那份**，不是内置的那份。所以价格真的涨了 20 倍以上时，
**同步一次就能到位**；之后基准跟着更新，不会把人永久锁在外面。

## schema 要点

```jsonc
{
  "provider": "deepseek",            // 必须是这个
  "displayName": "DeepSeek",
  "sourceUrl": "https://api-docs.deepseek.com/quick_start/pricing",
  "verifiedAt": "2026-09-12",        // YYYY-MM-DD，核对官方页面的日期
  "defaultCurrency": "CNY",          // 用户没选过货币时用哪个
  "notes": { "en": [...], "zh": [...] },   // About 面板显示，按语言键控

  "schedule": {
    "referenceUtcOffsetMinutes": 0,  // 时段以哪个时区定义（DeepSeek 用 UTC）
    "referenceLabel": "UTC",
    "weekly": [
      {
        "days": ["Mon","Tue","Wed","Thu","Fri"],   // 星期几也在这个参考时区里判定
        "windows": [["01:00","04:00"], ["06:00","10:00"]]  // 半开区间 [start, end)
      }
    ]
  },

  "models": [
    {
      "id": "deepseek-flash",
      "label": "V4.1 Flash",         // 表头显示的短名
      "version": "DeepSeek-V4.1-Flash",
      "prices": {
        "CNY": {                      // 以货币代码为键；官方直接公布两种货币，无汇率换算
          "inputCacheHit":  { "offPeak": 0.02, "peak": 0.04 },
          "inputCacheMiss": { "offPeak": 1,    "peak": 2    },
          "output":         { "offPeak": 4,    "peak": 8    }
        }
      }
    }
  ]
}
```

几个容易踩的点：

- **`windows` 是半开区间。** `["01:00","04:00"]` 表示 01:00:00 到 03:59:59，**04:00:00 整已属空闲**。
- **`end` 早于 `start` 即视为跨午夜**（如 `["22:00","02:00"]`）。
- **`end == start` 会被拒绝**，不是"整天"的意思。
- **星期几在 `referenceUtcOffsetMinutes` 指定的时区里判定**，不是用户的本地时区。
  对 UTC-5 的用户，周一 01:00 UTC 是当地**周日** 20:00——按本地判定会把整周错开一天。
- 秒级精度可写 `"01:30:15"`。
- 支持多条 `weekly` 规则，不同 `days` 可以有不同的 `windows`。

## 价格来源

<https://api-docs.deepseek.com/quick_start/pricing>

中文版与英文版对同一组数字的表述一致；两处的峰谷时段规则都核对过。
