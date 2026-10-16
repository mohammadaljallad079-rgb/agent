# SUPER AI AGENT — Windows Installer Acceptance (0.1.0-rc.2)

Candidate built from the merged `release/windows-rc-1` at merge commit
`8d2c759ea9c50a8ed2077191ca9910cffbc44a7a` (Gate 4A + Gate 4B voice + PR #4),
signed off on real Windows CI.

## 1. Artifact

| Field | Value |
|---|---|
| File | `ArabicAIAgent-Setup-0.1.0.exe` |
| Version | `0.1.0` |
| Size | 38,548,310 bytes |
| SHA-256 | `631c5ec455c1908964fb671c480b02da16827f82c56fc4ac82149d7dcbed3280` |
| Signing | **UNSIGNED** (no code-signing certificate) |
| Install scope | Per-user, `%LOCALAPPDATA%\ArabicAIAgent`, **no UAC / no admin** |
| Download | https://github.com/mohammadaljallad079-rgb/agent/releases/download/v0.1.0-rc.2/ArabicAIAgent-Setup-0.1.0.exe |
| Checksums | https://github.com/mohammadaljallad079-rgb/agent/releases/download/v0.1.0-rc.2/SHA256SUMS.txt |
| Release page | https://github.com/mohammadaljallad079-rgb/agent/releases/tag/v0.1.0-rc.2 |

This is a **prerelease**, not a production release.

## 2. Windows CI evidence (merge commit `8d2c759`)

- `Windows` push run **38925029832**, `Windows` pull_request run **38925037524** — both `success`.
- `Tests on Windows (3.11 / 3.12 / 3.13)` → **1085 passed, 60 skipped, 0 failed** each.
- `Build the Windows installer` → success. The job runs the full script
  `scripts/build_windows.ps1`: freeze → smoke-test → **frozen selfcheck** →
  compile `.iss` → hash → **install → selfcheck the installed app → uninstall**.
- Frozen selfcheck (build tree): `{"ok": true}` — `bundled_resources` ok,
  `document_generation` ok (Arabic **pdf 46769B / docx 36759B / pptx 29253B / xlsx 4941B**),
  `local_database` ok (13 tables migrated to head), `local_api` ok (`/local/status` 200),
  `voice_surface` **skipped** (runner has no audio device).
- Installed selfcheck (after the real silent install): `{"ok": true}`, same checks.
- The frozen payload bundles the window stack: pywebview 6.2.1, pythonnet 3.2.1,
  clr-loader, `WebView2Loader.dll` (and WebView2 managed DLLs).

## 3. تعليمات التثبيت بالعربية (Arabic installation instructions)

### قبل التثبيت

1. تأكّد أن جهازك Windows 10/11 بنواة 64-bit.
2. لا تحتاج صلاحيات المدير. لا تشغّل المثبّت كـ"مسؤول" — هذا قد يترك ملفات لا
   يستطيع حسابك الكتابة عليها لاحقًا.
3. أنزل الملف `ArabicAIAgent-Setup-0.1.0.exe` إلى مجلد محلي (مثل `Downloads`)، وليس
   على قرص شبكة.

### التحقّق من الملف (موصى به)

في PowerShell داخل مجلد التنزيل:

```powershell
Get-FileHash .\ArabicAIAgent-Setup-0.1.0.exe -Algorithm SHA256
```

يجب أن تكون القيمة:

```
631c5ec455c1908964fb671c480b02da16827f82c56fc4ac82149d7dcbed3280
```

إذا اختلفت القيمة، لا تثبّت الملف واحذفه.

### التثبيت

1. شغّل `ArabicAIAgent-Setup-0.1.0.exe` بنقرة مزدوجة.
2. لأن الملف **غير موقّع رقميًا**، قد يظهر تحذير من Windows:
   - إذا ظهرت نافذة **"Windows protected your PC"** (SmartScreen): اضغط
     **More info** ثم **Run anyway**. هذا متوقّع للملفات غير الموقّعة، ولا يعني
     أن الملف ضار. لا تُعطّل أي حماية أمنية للنظام لأجل ذلك.
   - إذا ظهر **"Unknown publisher"**: اضغط **Yes** للمتابعة.
3. أكمل خطوات المثبّت. التثبيت لمستخدمك الحالي فقط، ولن يطلب كلمة مرور المدير.
4. لا توجد خدمة خلفية ولا مهام مجدولة؛ البرنامج يعمل فقط عند تشغيله.
5. خيار **"Start when I sign in"** غير مُحدَّد افتراضيًا — اتركه دون تحديد إلا إذا
   أردت التشغيل التلقائي.

### التشغيل

- افتح البرنامج من قائمة **Start** باسم **Arabic AI Agent**.
- الواجهة عربية من اليمين إلى اليسار افتراضيًا.
- التشغيل من الطرفية (اختياري): `ArabicAIAgent.exe selfcheck` للتحقّق،
  و`ArabicAIAgent.exe version` لطباعة الإصدار.

### إلغاء التثبيت

من **Settings → Apps → Installed apps → Arabic AI Agent → Uninstall**، أو من
مجلد التثبيت `unins000.exe`. الإلغاء يسألك هل تريد الاحتفاظ ببيانات المستخدم.

### إذا ظهرت رسالة "Access is denied"

هذا ليس خطأ صلاحيات مدير. الأسباب المتوقّعة:

1. **البرنامج لا يزال يعمل** — نسخة قيد التشغيل تُبقي الملفات مقفلة. أغلق البرنامج
   من شريط المهام (بما في ذلك أي أيقونة في منطقة الإشعارات) ثم أعد المحاولة.
   الإصدار 0.1.0-rc.2 يمنع تشغيل نسختين في الوقت نفسه، والمثبّت ينتظر إغلاق
   النسخة العاملة بدل أن يفشل.
2. **مجلد تثبيت قديم مملوك لحساب آخر** — احذف `%LOCALAPPDATA%\ArabicAIAgent` من
   حسابك ثم أعد التثبيت.
3. **مضاد الفيروسات** يحجر الملف غير الموقّع، أو **Controlled Folder Access** يمنع
   الكتابة. اسمح بالتطبيق، أو وقّع الملف لاحقًا. **لا تُعطّل الحماية الأمنية.**
4. **Mark-of-the-Web** — إذا نُسخ الملف من قرص شبكة أو جهاز آخر، انسخه إلى مجلد
   محلي، وإن لزم طبّق `Unblock-File`.

## 4. قائمة القبول على Windows (Windows acceptance checklist)

شغّل كل بند وسجّل النتيجة الحقيقية: `PASS` / `FAIL` / `NOT VERIFIED`. البنود
المعلَّمة **[تفاعلي]** تحتاج سطح مكتب Windows حقيقيًا مسجَّل الدخول.

| # | البند | كيف تتحقّق | النتيجة المتوقّعة |
|---|---|---|---|
| 1 | التثبيت | شغّل المثبّت | ينتهي دون خطأ، بلا طلب صلاحيات مدير |
| 2 | التحقّق من البصمة | `Get-FileHash` | يطابق SHA-256 أعلاه |
| 3 | فتح التطبيق | افتح من Start | تظهر نافذة التطبيق |
| 4 | الواجهة العربية | انظر الواجهة | عربية، من اليمين إلى اليسار |
| 5 | تسجيل الميكروفون **[تفاعلي]** | قسم المساعد الصوتي → استمع | يسجّل مقطعًا، أو يشرح السبب بدقة |
| 6 | النسخ العربي **[تفاعلي]** | استمع لمدّة قصيرة | يظهر النص في "ما سمعته" |
| 7 | تشغيل الصوت العربي **[تفاعلي]** | "انطق الردّ" | يُسمع ردّ عربي |
| 8 | أمر صوتي ينتج PDF **[تفاعلي]** | اطلب تقريرًا ونفّذه | يُنشأ ملف PDF فعلي في مساحة العمل |
| 9 | نسخة ثانية لا تُفتح | شغّل البرنامج مرتين | النسخة الثانية تخرج بهدوء |
| 10 | العمل دون اتصال **[تفاعلي]** | افصل الشبكة ثم استخدم أداة لا تحتاج شبكة | تعمل؛ نماذج LLM المحلية تحتاج تنزيلًا مسبقًا |

## 5. وظائف غير قابلة للتحقّق آليًا (hardware-dependent, explicitly unverified)

هذه لم تُختبر على CI؛ تحتاج جهازًا حقيقيًا. لا تُعدّ ناجحة أو فاشلة قبل تجربتها:

- **تسجيل الميكروفون** (`MICROPHONE_RECORD`) والنسخ الصوتي عبر Whisper — تحتاج
  ميكروفونًا حقيقيًا؛ وCI يبلّغ `voice_surface` كـ `skipped` (لا جهاز صوت).
- **تشغيل الصوت العربي** عبر SAPI5 — يحتاج صوتًا عربيًا مثبّتًا في النظام
  (`ar_JO-kareem` هو صوت Piper العربي الوحيد؛ أصوات SAPI تعتمد على حِزم اللغة).
- **التشغيل دون اتصال بالمعنى الكامل** — المثبّت لا يضمّ محرّك النموذج المحلي
  (`llama-cpp-python` بلا wheel جاهز لـ Windows). الاستدلال المحلي يتطلّب تنزيل
  النموذج من داخل التطبيق أولًا؛ بدون ذلك يعمل التطبيق عبر المسار السحابي.
- **الطباعة، التقاط الشاشة، تعدد الشاشات، DPI، البريد/ERP/WhatsApp** — لم تُنفَّذ
  أو لم تُختبر في هذه المرحلة.

## 6. الحكم (Verdict)

كل ما يمكن التحقّق منه آليًا على Windows **ناجح** (CI أخضر، selfcheck `ok`،
تثبيت/إلغاء حقيقي). البنود الصوتية وبعض تكاملات العتاد تبقى **[تفاعلي]** ولم
تُتحقّق بعد.

**READY FOR USER ACCEPTANCE** — يبدأ المستخدم البنود التفاعلية (5–8 و10).

## 7. ملاحظة أمنية / blockers

- **لا blocker برمجي** يمنع التثبيت أو التشغيل.
- الملف **غير موقّع**: توقّع SmartScreen. لا يوجد حل برمجي؛ الحل شراء شهادة توقيع
  (خارج نطاق هذه المرحلة) ثم إعادة البناء.
- **لم يُنشر** أي إصدار إنتاجي عام. `v0.1.0-rc.2` **prerelease**.
