# Power BI и Tableau на данных PayOps: курс с практикой

Курс рассчитан на то, чтобы научиться с нуля и получить отчёт, который не стыдно показать на собеседовании.
Все данные синтетические, их можно смело публиковать.

В исторические данные заранее спрятано 6 инцидентов. Задача каждого урока — найти очередной инцидент
средствами BI. Ответы лежат в дашборде: **BI tools → Learn → answer**.

---

## 0. Как подключиться: 4 способа

| Способ | Tableau Desktop | Tableau Public (бесплатный) | Power BI Desktop (бесплатный) | Excel |
|---|---|---|---|---|
| **PostgreSQL напрямую** (живые данные) | да, файл `.tds` | нет | да, файл `.pbids` (Import или DirectQuery) | через ODBC, сложно |
| **Extract `.hyper`** (снимок данных) | да | **да** | нет | нет |
| **CSV по HTTP** (Web) | да, как Text file | да, скачать CSV | да, `.pbids` Web | да, From Web |
| **OData** (все таблицы сразу) | да | да | да | да |

Все файлы скачиваются на странице **BI tools** в дашборде (http://localhost:8080/#bi).

Параметры базы:

```
Server:   localhost
Port:     5432
Database: opanalytics
User:     analyst      (только чтение)
Password: analyst
```

Перед началом:

```
python opa.py up
python opa.py seed          # 500k транзакций за 30 дней + 6 инцидентов
```

На странице BI tools нажмите **Check access**. Должно быть написано, что пользователь `analyst` видит все 9 датасетов.

### Какие таблицы брать

| Таблица | Что в ней | Для чего |
|---|---|---|
| `v_transactions` | одна строка = одна попытка оплаты, есть флаги `is_approved`, `is_declined`, `is_error`, `is_refunded` | основная таблица фактов |
| `merchants`, `providers` | справочники | измерения |
| `v_hourly_provider`, `v_daily_kpis`, `v_daily_merchant`, `v_country_provider` | готовые агрегаты | быстрые графики, если основная таблица тормозит |
| `anomalies` | что нашёл детектор | сверка своих находок |
| `seed_ground_truth` | что было заложено | ответы (лучше не открывать раньше времени) |

---

## Часть 1. Power BI Desktop

Power BI Desktop бесплатный и работает только на Windows. Скачать его можно в Microsoft Store («Power BI Desktop»)
или на microsoft.com/power-bi. Учётная запись для Desktop не нужна, она требуется только для публикации в облако.

### Урок 1. Подключение

1. На странице BI tools → вкладка Power BI → **Import .pbids**. Откройте скачанный файл двойным щелчком.
2. Power BI спросит учётные данные: вкладка **Database** (не Windows), `analyst` / `analyst`.
3. Если появится вопрос про шифрование (encrypted connection), нажмите **OK**: у нас локальная база без SSL.
4. В окне **Navigator** отметьте `public.v_transactions`, `public.merchants`, `public.providers`,
   `public.v_daily_merchant`, `public.anomalies` и нажмите **Transform Data**, а не Load.

Если PostgreSQL недоступен, есть запасной вариант: Get data → **OData feed** → `http://localhost:8080/odata`, Anonymous.

### Урок 2. Power Query — подготовка данных

Power Query — это редактор, где данные чистят до загрузки в модель. Каждое действие записывается как шаг
(Applied Steps), позже их можно посмотреть на языке M: Home → Advanced Editor.

1. В `v_transactions` проверьте типы колонок. `created_at` должен быть Date/Time/Timezone, `amount_usd` — Decimal.
2. Добавьте колонку даты: выделите `created_at` → Add Column → Date → Date Only. Назовите её `date`.
3. Добавьте колонку часа: Add Column → Custom Column:
   `DateTime.From(Date.From([created_at])) + #duration(0, Time.Hour([created_at]), 0, 0)`, имя `hour`.
4. Close & Apply.

Дальнейшее чтение: queries.pq на странице BI tools, там готовый M-код для всех таблиц.

### Урок 3. Модель данных

1. Слева откройте вид **Model** (третья иконка).
2. Перетащите `v_transactions[merchant_id]` на `merchants[merchant_id]`. Получится связь многие-к-одному (*:1).
3. Так же свяжите `v_transactions[provider]` с `providers[provider_id]`.
4. Создайте таблицу дат: Modeling → New table:

   ```
   Calendar = CALENDAR ( MIN ( v_transactions[date] ), MAX ( v_transactions[date] ) )
   ```
   Свяжите `Calendar[Date]` с `v_transactions[date]`. Затем Table tools → Mark as date table.

Такая схема называется «звезда»: одна таблица фактов и несколько справочников вокруг. Это самый важный
паттерн в Power BI, его часто спрашивают на собеседованиях.

### Урок 4. Меры DAX

Скачайте **measures.dax**. Создавайте меры по одной: Modeling → New measure. В файле таблица названа
`Transactions`, а у вас она `v_transactions`, поэтому имя нужно заменить или переименовать таблицу
(двойной щелчок по ней в списке Data).

Что важно понять:

- **Мера** считается заново для каждого фильтра (ячейки, точки графика). В этом её отличие от колонки,
  которая считается один раз для каждой строки.
- `DIVIDE(a, b)` — деление, которое не падает при делении на ноль.
- `CALCULATE(выражение, фильтр)` — та же мера, но с изменённым фильтром. Например,
  `Approval Rate Baseline` убирает фильтр по дате и даёт «нормальный» уровень для сравнения.

Минимальный набор: `Transactions`, `Approval Rate`, `Decline Rate`, `Error Rate`, `Refund Rate`,
`Avg Processing ms`, `Conversion`.

### Урок 5. Первая страница отчёта и задание №1

1. Добавьте **Card** для каждой ключевой меры: Approval Rate, Transactions, Avg Processing ms, Refund Rate.
   Для процентов: выделите меру → Measure tools → Format → Percentage.
2. **Line chart**: X = `hour`, Y = `Approval Rate`, Legend = `providers[name]`.
3. Добавьте **Slicer** по `country` и по `payment_method`.

**Задание 1.** Найдите провайдера, у которого approval rate внезапно упал. Когда это было и насколько сильно?
Наведите мышь на провал и посмотрите tooltip.

**Задание 2.** Сделайте такой же график для `Avg Processing ms`. Какой провайдер стал медленным и сколько это длилось?

### Урок 6. Матрица с условным форматированием, задание №3

1. Visual **Matrix**: Rows = `merchants[name]`, Columns = `Calendar[Date]`, Values = `Refund Rate`.
2. Values → Refund Rate → Conditional formatting → Background color, градиент от белого к красному.

**Задание 3.** У какого мерчанта аномально высокий refund rate и в какие дни?

### Урок 7. Детализация (drill-down), задание №4

1. Matrix: Rows = `country`, Columns = `Calendar[Date]`, Values = `Approval Rate` с условным форматированием.
2. Найдите самую «красную» ячейку. Добавьте рядом **Bar chart**: Y = `decline_reason`, X = `Transactions`.
3. Кликните на ячейку матрицы — bar chart отфильтруется (это cross-filtering).

**Задание 4.** В какой стране был плохой день и какая причина отказов тогда доминировала?

### Урок 8. Ошибки и пропажа трафика, задания №5 и №6

**Задание 5.** График `Error Rate` по часам с легендой по провайдерам. У кого были технические ошибки?
Посмотрите на `decline_reason` в это время: `provider_timeout` или `provider_unavailable`?

**Задание 6.** Stacked column chart: X = `hour`, Y = `Transactions`, Legend = `merchants[name]`, или лучше
small multiples по мерчантам. У какого мерчанта трафик на несколько часов почти исчез?

Подсказка для 6: удобнее считать долю трафика мерчанта среди всех транзакций за час:
```
Traffic Share = DIVIDE ( [Transactions], CALCULATE ( [Transactions], REMOVEFILTERS ( merchants ) ) )
```

### Урок 9. Import против DirectQuery — живые данные

1. Скачайте **DirectQuery .pbids** и откройте как новый отчёт.
2. В дашборде запустите трафик (Traffic & routing → Start), в Chaos lab нажмите **AlphaPay declines +30pp**.
3. Сделайте график Approval Rate по провайдерам за последний час (фильтр по `created_at`).
4. Через пару минут нажмите Refresh: провал AlphaPay появится в отчёте.

Разница: **Import** хранит копию данных в файле .pbix (быстро, но обновляется вручную или по расписанию).
**DirectQuery** каждый раз ходит в базу (данные всегда свежие, но отчёт медленнее, а часть функций DAX недоступна).

### Урок 10. Что дальше

- Bookmarks и кнопки для навигации между страницами отчёта.
- Drill-through страница «карточка мерчанта».
- Публикация: для Power BI Service нужен рабочий или учебный аккаунт Microsoft. Для портфолио можно выложить
  .pbix на GitHub и сделать скриншоты.

---

## Часть 2. Tableau

**Tableau Public** бесплатный (public.tableau.com), но он не подключается к базам. Используйте файл
**payops.hyper** или CSV. Готовые работы публикуются в ваш профиль Tableau Public — это отличная ссылка для резюме.

**Tableau Desktop** платный, есть пробная версия на 14 дней и бесплатная лицензия для студентов
(Tableau for Students). Он умеет живое подключение к PostgreSQL.

### Урок 1. Подключение

**Tableau Public:** BI tools → вкладка Tableau → **payops.hyper**. Далее Tableau Public → Connect →
To a File → More... → выберите файл.

**Tableau Desktop:**
1. Установите драйвер PostgreSQL: tableau.com/support/drivers → PostgreSQL → скачайте .jar в
   `C:\Program Files\Tableau\Drivers`.
2. Скачайте `payops_transactions.tds` и откройте двойным щелчком. Пароль: `analyst`.
   Или вручную: Connect → To a Server → PostgreSQL → localhost / 5432 / opanalytics / analyst / analyst,
   «Require SSL» не отмечать.

### Урок 2. Data Source и связи

1. На вкладке Data Source перетащите `transactions` (или `v_transactions`) на холст.
2. Перетащите рядом `merchants` и `providers`. Tableau создаст **relationship**. Настройте поля:
   `merchant_id = merchant_id`, `provider = provider_id`.
3. Внизу проверьте типы. `created_at` — Date & Time, `amount_usd` — Number (decimal).

Relationship (логический слой) в отличие от join не дублирует строки: Tableau сам решает, как соединять таблицы для каждого графика.

### Урок 3. Calculated fields

Analysis → Create Calculated Field. Создайте:

```
Approval Rate   SUM([Is Approved]) / COUNT([Transaction Id])
Decline Rate    SUM([Is Declined]) / COUNT([Transaction Id])
Error Rate      SUM([Is Error]) / COUNT([Transaction Id])
Refund Rate     SUM([Is Refunded]) / SUM([Is Approved])
Approved USD    SUM(IF [Status] = "approved" THEN [Amount Usd] END)
Conversion      COUNTD(IF [Is Approved] = 1 THEN [Payment Id] END) / COUNTD([Payment Id])
```

Для процентов: правый клик по полю → Default Properties → Number Format → Percentage.

**LOD-выражение** — «нормальный» уровень провайдера за всё время, независимо от фильтров на листе:
```
Provider Baseline   {FIXED [Provider] : SUM([Is Approved]) / COUNT([Transaction Id])}
Drop vs Baseline    [Approval Rate] - MIN([Provider Baseline])
```

### Урок 4. Первый лист и задания №1–2

1. Columns: `Created At` → правый клик → **Exact Date**, затем выберите детализацию Hour (непрерывная, зелёная).
2. Rows: `Approval Rate`. Color: `Provider`.
3. Analytics → **Reference Line** со средним значением.

**Задание 1:** найдите провайдера с внезапным падением approval rate.
**Задание 2:** замените меру на `AVG([Processing Time Ms])` и найдите медленного провайдера.

### Урок 5. Heat map, задания №3–4

1. Rows: `Merchant Name`, Columns: `DAY(Created At)`, Color: `Refund Rate`, Marks: Square.
   **Задание 3:** у кого refund wave?
2. Rows: `Country`, Columns: `DAY(Created At)`, Color: `Approval Rate` (палитра Red-Green Diverging).
   **Задание 4:** какая страна и какой `Decline Reason` доминировал? Перетащите `Decline Reason` в Filters
   или сделайте отдельный bar chart.

### Урок 6. Dashboard с фильтрами, задания №5–6

1. New Dashboard → перетащите листы. Для одного из листов: Use as Filter (иконка воронки).
   Клик по провайдеру отфильтрует остальные графики.
2. **Задание 5:** лист Error Rate по часам и провайдерам.
3. **Задание 6:** лист `COUNT(Transaction Id)` по часам, Color = Merchant Name, или Rows = Merchant Name
   (small multiples). Найдите провал.

### Урок 7. Публикация (Tableau Public)

File → Save to Tableau Public. Нужен бесплатный аккаунт. Получится ссылка вида
`public.tableau.com/app/profile/<вы>/viz/...` — её можно вставить в резюме.

---

## Сверка с детектором

После того как вы нашли инциденты сами:

1. Подключите таблицу `anomalies` и посмотрите, что нашёл Python-детектор (колонки `metric`, `dim_value`,
   `window_start`, `window_end`, `correlated_with`).
2. Сравните с `seed_ground_truth` — там то, что было заложено на самом деле.
3. Что детектор нашёл лишнего (`correlated_with` не пустой)? Почему, например, у мерчанта m_012 тоже упал approval
   в тот день, когда падала Бразилия?

---

## Если что-то не работает

| Проблема | Решение |
|---|---|
| Power BI: «The remote certificate is invalid» / вопрос про шифрование | Нажать OK (подключиться без шифрования) |
| Power BI: ошибка логина | Вкладка **Database**, а не Windows; `analyst` / `analyst` |
| Tableau Desktop: «driver required» | Скачать драйвер PostgreSQL (.jar) в `C:\Program Files\Tableau\Drivers` |
| Не подключается вообще | `python opa.py status` — postgres должен быть `up`, порт 5432 |
| payops.hyper не скачивается (501) | `.venv\Scripts\pip install -r requirements-bi.txt`, затем `python opa.py restart dashboard` |
| DirectQuery очень медленный | Используйте агрегаты `v_hourly_provider`, `v_daily_*` вместо `v_transactions` |
| В отчёте нет свежих данных | Import: Home → Refresh. Extract (.hyper): скачать заново |

---

## Как рассказать об этом на собеседовании

- «Подключил Power BI к PostgreSQL. Модель — звезда (факт transactions + справочники + календарь). Меры на DAX:
  approval / decline / refund rate, conversion с учётом каскадных ретраев, отклонение от baseline через CALCULATE + REMOVEFILTERS.»
- «В Tableau сделал дашборд с LOD-выражениями для baseline провайдера и heat map refund rate по мерчантам.»
- «Нашёл 6 инцидентов: падение approval у провайдера, рост задержек, refund wave у мерчанта, падение
  success rate в стране, таймауты, пропажа трафика мерчанта. Сверил свои находки с Python-детектором.»
