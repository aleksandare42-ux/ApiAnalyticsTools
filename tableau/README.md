# Tableau: дашборд платіжних операцій

Файли підключення (`.tds`, `.hyper`) знаходяться на сторінці **BI tools** дашборду,
а покроковий курс — у [../bi/GUIDE_RU.md](../bi/GUIDE_RU.md).

## Підключення

**Живе підключення (рекомендовано):** Tableau Desktop → *Connect → PostgreSQL*

| Поле | Значення |
|---|---|
| Server | `localhost` |
| Port | `5432` |
| Database | `opanalytics` |
| Username / Password | `analyst` / `analyst` (лише читання) |

**Tableau Public (без конектора PostgreSQL):** у дашборді відкрийте *Dataset & export* і завантажте CSV-файли (`hourly_provider.csv`, `daily_kpis.csv`, `daily_merchant.csv`, `transactions.csv`, `anomalies.csv`) або викличте `GET http://localhost:8080/api/export/<name>.csv?window=30d`.

## Джерела даних

| Представлення | Гранулярність | Для чого |
|---|---|---|
| `v_transactions` | спроба | для всього; містить прапорці `is_approved`, `is_declined`, `is_error`, `is_refunded`, а також назви мерчантів і провайдерів |
| `v_hourly_provider` | година × провайдер | Provider Performance, тренд затримки |
| `v_daily_kpis` | день | шапка з KPI |
| `v_daily_merchant` | день × мерчант | аномалії частки повернень |
| `v_country_provider` | країна × провайдер | теплова карта |
| `anomalies` | інцидент | позначки / таблиця аномалій |

## Обчислювані поля (на `v_transactions`)

```
Payment Success Rate   SUM([Is Approved]) / COUNT([Transaction Id])
Decline Rate           SUM([Is Declined]) / COUNT([Transaction Id])
Error Rate             SUM([Is Error])    / COUNT([Transaction Id])
Refund Rate            SUM([Is Refunded]) / SUM([Is Approved])
Conversion             COUNTD(IF [Is Approved] = 1 THEN [Payment Id] END) / COUNTD([Payment Id])
Approved Volume USD    SUM(IF [Is Approved] = 1 THEN [Amount Usd] END)
Avg Processing Time    AVG([Processing Time Ms])
Success Rate Δ vs 7d   [Payment Success Rate] - WINDOW_AVG([Payment Success Rate], -168, -1)   // погодинний табличний розрахунок
```

## Рекомендовані аркуші

1. **Шапка з KPI**: частка успішних платежів, частка відмов, обсяг, середній час обробки, частка повернень (BAN-показники з % зміни відносно попереднього періоду).
2. **Payment Success Rate у часі за провайдерами**: лінійний графік, погодинно. Добре видно падіння AlphaPay і день BR.
3. **Provider Performance**: таблиця з часткою схвалень, часткою помилок, середньою/p95 затримкою та обсягом для кожного провайдера.
4. **Average Processing Time**: погодинна лінія для кожного провайдера. Показує плато затримки BetaGate.
5. **Transaction Volume**: стовпчики з накопиченням за статусом. Відфільтруйте за мерчантом, щоб побачити провал MelodyBox (m_009).
6. **Refund rate за мерчантами**: щоденна теплова таблиця з `v_daily_merchant`. LuvMatch (m_017) виділяється.
7. **Anomalies**: таблиця `anomalies`, з'єднана за `dim_value`, показана як опорні смуги (`window_start` → `window_end`) на графіках 2 і 4.
