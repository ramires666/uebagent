# Антибот-тесты — 31 октября 2026

**Состояние отчёта: RUNNING · итог по production: NOT CONFIRMED**

Это текущий журнал ограниченных публичных гостевых сценариев и браузерных диагностик. Результаты ниже заполняются только после проверки соответствующих evidence. По Sannysoft Profile A подтверждена загрузка страницы и получены частичные диагностические признаки; статус предварительный `PARTIAL`. Для Profile A доступны страницы всех шести сайтов, однако ни один полный тестовый путь не завершён; результаты диагностики и guest flow остаются `PARTIAL` или `NOT_RUN` по матрице. Profile B — `NOT_RUN`. Общего PASS нет.

## Среда прогона

- Изолированный кандидат: `browser-mcp-antibot-2026-10-31`; browser 2816 MiB, maintenance включён, headed; control 768 MiB.
- Созданы отдельный volume профиля, tmpfs control и новый test key. Viewport 1440×900.
- Тестовый viewer: `http://localhost:18081/desktop` — HTTP 200; `ready=true`, 1 context, 1 page, `owner=none`. Независимый GET state на момент проверки подтвердил `available=true`, ожидаемый Sannysoft URL.
- Production `:8090` наблюдался отдельно: `ready=true`, 1 human context, 1 page. Это только снимок состояния; production не является тестовой средой и его готовность к выпуску не подтверждена.

```text
PRODUCTION :8090                         ИЗОЛИРОВАННЫЙ TEST :18081
ready=true · human 1 context / 1 page    viewer HTTP 200 · ready=true
НЕ ИСПОЛЬЗУЕТСЯ ДЛЯ ПРОГОНА              candidate · browser 2816 MiB
                                         maintenance on · headed
                                         control 768 MiB
                                         отдельные profile volume / tmpfs control / test key
                                         1 context · 1 page · owner none · 1440×900
```

## Матрица сайтов и профилей

Статус каждой ячейки отражает результат именно указанного теста. `Доступна` означает лишь подтверждённую загрузку страницы; это не равно завершённой диагностике или полному гостевому сценарию.

| Сайт / тип | Profile A | Profile B | Что требуется для PASS |
|---|---|---|---|
| Sannysoft · диагностика | PARTIAL — страница загружена, сигналы неполны | NOT_RUN | WebDriver: missing (passed); Chrome: missing (failed); permissions: prompt. WebDriver Advanced outcome не подтверждён; plugins label есть, outcome не подтверждён; languages label есть, значение не сохранялось. Challenge text не обнаружен. Это не общий PASS. |
| BrowserLeaks · диагностика | PARTIAL — JavaScript и WebGL проверены, обязательный брендовый элемент отсутствует | NOT_RUN | Expected URL true; exact `BrowserLeaks.com` label false; JavaScript и WebGL labels true; challenge strings false. Диагностика частичная, не PASS. |
| EFF Cover Your Tracks · диагностика | PARTIAL — landing page доступна, тест не запускался | NOT_RUN | Один CTA `Test Your Browser` нажат; пять aggregate heading checks false. Страница результата `/kcarter` открыта; завершённый aggregate результат пока не подтверждён. Challenge не обнаружен. Статус `PARTIAL`; не оценка WAF. |
| Amazon · гостевой путь | PARTIAL — landing page доступна, guest flow не завершён | NOT_RUN | Expected URL true; `Amazon` marker true; `Search` marker false; challenge strings false. Статус авторизации аккаунта ещё не проверен; guest flow не запускать до проверки signed-out состояния. |
| eBay · гостевой путь | PARTIAL — страница и маркеры доступны, guest flow не исполнен | NOT_RUN | Expected URL и eBay/Search markers true; challenge strings false. Статус авторизации ещё не проверен; до guest flow подтвердить signed-out. |
| DemoQA · гостевой путь | PASS — ограниченный read-only путь | NOT_RUN | Home → Elements → history back; expected URL и ожидаемые headings подтверждены, challenge false. DemoQA brand marker на home отсутствовал, но не входит в критерий пути; первая PARTIAL была из-за лишнего harness predicate. Это не антибот-тест само по себе. |

**Временные метки и среда P1:** время начала не записано; время наблюдения после перехода — 2026-10-30 20:12:06 UTC. Часовой пояс браузера не измерялся. Браузерная версия API не раскрыта. Не выводить TZ браузера из системного timezone.

**Текущий учёт:** BrowserLeaks Profile A — предварительный `PARTIAL`: expected URL true; exact `BrowserLeaks.com` label false; JavaScript и WebGL labels true; challenge strings false. Sannysoft Profile A — предварительный `PARTIAL`: независимый GET test viewer подтвердил `available=true`, ожидаемый URL, `owner=none`, 1 context и 1 page. Выполнен один переход без challenge. Получены только перечисленные выше частичные predicates; дополнительные результаты ещё не проверены. **Сводка P1:** 1 PASS (DemoQA ограниченный read-only путь), 5 PARTIAL (Sannysoft, BrowserLeaks, EFF, Amazon, eBay), 0 BLOCKED/UNAVAILABLE/FAIL; все шесть страниц доступны. EFF CTA нажат один раз, но aggregate result остаётся непроверенным. Amazon/eBay guest flows не завершены и signed-out status ещё нужно подтвердить. Profile B всех сайтов — `NOT_RUN`; production — `NOT CONFIRMED`.

## Как читать выводы

- **Доступность страницы** подтверждает загрузку и ожидаемый origin. Она не подтверждает, что диагностический тест завершился, что весь путь выполнен, или что сайт допускает более широкий сценарий.
- **Диагностика** описывает только перечисленные свойства браузера/приватности. Её оценки не доказывают прохождение WAF и не объединяются с гостевыми путями в «антибот PASS».
- **Гостевой путь** считается PASS только после всех заданных шагов и независимой проверки ожидаемого состояния. Любой challenge, CAPTCHA, требование входа, rate limit или запрет останавливает тест; статус `BLOCKED`, без повторов.
- **Детерминированный проход** и **модельный read-only проход** — отдельные сценарии. Модельный сценарий не заменяет verifier; пока его результат `NOT_RUN`/не представлен.
- **Production:** состояние сервиса на `:8090` не означает, что этот тестовый кандидат установлен там или что релиз готов. Итоговый production статус: **NOT CONFIRMED**.

## Ограничения и следующие шаги

Проверенное evidence по Sannysoft P1 подтверждает частичную доступность страницы и ограниченный набор диагностических признаков. Независимый raw-verify proof не сохранён, поэтому воспроизводимость row predicates ограничена сообщением исполнителя и metadata; статус остаётся `PARTIAL`. Временный full-page PNG `/tmp/antibot-artifacts/full-page-temporary.png` удалён исполнителем; проверка `test ! -e` прошла. Сохранённый row-level raw-verify proof отсутствует, поэтому воспроизводимость конкретных predicate ограничена. На опубликованном безопасном banner проблема приватности не обнаружена. BrowserLeaks JS+WebGL, EFF aggregate result неполон; Amazon/eBay signed-out не проверен; guest paths не завершены. DemoQA PASS ограничен указанным read-only путем. Profile B не оценены. Доступность шести страниц не означает завершение шести тестов. Копии evidence для новых результатов ожидаются и будут связаны после чтения. Никаких общих выводов делать нельзя. Сохранить для отчёта только безопасные сводки и скриншоты: не публиковать IP, полные fingerprint-значения, cookies, токены, заголовки авторизации или личные данные.

1. Перед guest flow Amazon/eBay проверить signed-out состояние в чистых профилях; не продолжать при неизвестном или авторизованном аккаунте.
2. Для Amazon/eBay подтвердить signed-out состояние в предназначенных чистых профилях; начать guest flow только после этого.
3. Отдельно отметить ручной headed-контроль, если он выполнен, и ограничения сравнения.
4. Модельный read-only проход учитывать отдельно и только при наличии независимого verifier evidence.
5. Зафиксировать незавершённые/остановленные сценарии, ссылки на проверенные артефакты и открытые ограничения. Не делать вывод о production readiness по этому отчёту.

## Evidence

EFF metadata timestamp относится к post-click helper состоянию, не интерпретировать как точное время начала. DemoQA: первая PARTIAL была вызвана лишней проверкой brand marker; исправленные критерии дали ограниченный PASS.

Sannysoft P1: [metadata](evidence/sannysoft/p1/metadata.json) и [page-banner.png](evidence/sannysoft/p1/page-banner.png). BrowserLeaks: [JavaScript](evidence/browserleaks/p1/javascript-p1.json), [WebGL](evidence/browserleaks/p1/webgl-p1.json). [EFF](evidence/eff/p1-metadata.json) (CTA один раз; summary completion не подтверждён), [Amazon](evidence/amazon/p1-metadata.json), [eBay](evidence/ebay/p1-metadata.json), [DemoQA](evidence/demoqa/p1-metadata.json). EFF metadata `started_at_utc` содержит post-click timestamp, не точное время начала. DemoQA PASS ограничен URL/headings/history-back и не включает clicklink или modeltest. Amazon/eBay metadata не подтверждает signed-out статус; проверить перед guest flow.
