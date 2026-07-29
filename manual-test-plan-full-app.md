# Manual Test Plan — Full App

Covers every deployed feature: Announcements, Homework, Homework Submission, Calendar (holidays & days off), Guardian ↔ Teacher Messaging, Class Group Messaging, the Per-School Messaging Switch, Fee Notices, Permission Slips, Attendance Tracking, and Report Cards per Student — plus two cross-cutting sections (Multi-Guardian Households, Multi-School/Multi-Role) that re-check the same permission boundary across all of the above.

Push notifications are **Planned**, not yet built — not covered here.

Run this on a throwaway test school. Check off each box; anything that doesn't match "Expected" is a bug. Exact button labels, page titles, and URLs are called out so you can follow this without reading the code.

---

## 0. Setup

- [ ] **0.1** As a superuser, set up **School A** (a fresh test school). Create admin **Admin A**.
- [ ] **0.2** In School A, create two teachers: **Teacher A1** and **Teacher A2**.
- [ ] **0.3** Create two classes: **Class 1** (homeroom = Teacher A1) and **Class 2** (homeroom = Teacher A2). Teacher A1 is not a co-teacher on Class 2 and vice versa.
- [ ] **0.4** Create four students in Class 1: **Student 1**, **Student 2**, **Student 3**, **Student 4**, all active.
- [ ] **0.5** Create guardian accounts: **Guardian 1** and **Guardian 2**.
  - Link Guardian 1 to Student 1 only.
  - Link Guardian 2 to Student 1 *and* Student 2 (a two-guardian household on Student 1, and a second unrelated child for Guardian 2).
- [ ] **0.6** As the same superuser, set up a second school, **School B**, with its own admin **Admin B**, at least one class, and one student — used later for cross-school checks.
- [ ] **0.7** Note today's date and a date a few days in the past for date-based tests.

---

## 1. Announcements

- [ ] **1.1** As **Admin A**, open **Announcements** (`core:announcements`). Click **"New Announcement"**. Leave the class field blank (school-wide) and save. Confirm it's created but not yet published (status tag shows **"Draft"**).
- [ ] **1.2** Create a second announcement, this time scoped to **Class 1**. Save as draft.
- [ ] **1.3** Publish the school-wide announcement (row action toggles to **Published**). Leave the Class 1 one as a draft.
- [ ] **1.4** As **Teacher A2** (who doesn't teach Class 1), confirm both announcements are visible in the admin/teacher list, including the still-draft one (drafts are visible to any staff, just not to guardians).
- [ ] **1.5** As **Guardian 1** (linked to a Class 1 student), open Announcements. Confirm the published school-wide one shows, the draft Class 1 one does **not**, and there's a **"Mark as read"** button on the unread one.
- [ ] **1.6** Click "Mark as read". Confirm the tag changes to **"Read"** and it persists across a page reload/re-login.
- [ ] **1.7** As **Guardian 2** with a child in Class 1 too, publish the Class 1-scoped announcement (as Admin A first), then confirm Guardian 2 now sees it, but a guardian with no children in Class 1 would not (spot-check by temporarily unlinking, or reason through the visibility rule).
- [ ] **1.8** Try to reach `announcement_mark_read` for a still-draft announcement's id directly as a guardian (edit the URL). Confirm it 404s rather than marking a draft as read.
- [ ] **1.9** Delete an announcement as Admin A. Confirm the delete button shows a confirmation dialog ("This can't be undone"), and after confirming, it's gone from every list (a hard delete, not an archive).
- [ ] **1.10** As **Guardian 1**, confirm you cannot reach `announcement_new`/`announcement_edit`/`announcement_delete` — 403 on each.

---

## 2. Homework

- [ ] **2.1** As **Teacher A1**, open **Homework** (`core:homework`) and click **"New Homework"**/equivalent. Create an item for Class 1 with a due date and an attachment (any file type — e.g. a `.docx` or `.zip` — should be accepted; only Homework Submission restricts file types, not the teacher's own attachment).
- [ ] **2.2** Try uploading a file just over 10MB as the teacher attachment. Confirm it's rejected with a clear size-limit error.
- [ ] **2.3** Confirm the homework appears immediately for Class 1's guardians — there's no draft/publish step for Homework (unlike Announcements).
- [ ] **2.4** Open the homework's detail page. Confirm the tag shows **"Not accepting submissions"** (off by default) and a note explaining how to turn it on.
- [ ] **2.5** Edit the homework and turn on **"Accepts submissions"**. Confirm the detail page tag flips to **"Accepting submissions"**.
- [ ] **2.6** As **Guardian 2** (no child in Class 1... wait, Guardian 2 does have Student 2 in Class 1 — use Guardian 1 or a guardian with no linked child in Class 1 instead, or a child in Class 2), confirm homework for Class 1 does not show up for a guardian with no child in that class.
- [ ] **2.7** Delete the homework as Teacher A1. Confirm it's a hard delete (no archive/restore option), and it's gone from the guardian list too.

---

## 3. Homework Submission

- [ ] **3.1** Ensure a homework item exists for Class 1 with **"Accepts submissions"** on and a due date in the future.
- [ ] **3.2** As **Guardian 1**, open the homework in the guardian homework list. Confirm a per-child submission row for Student 1 shows status **"Not yet submitted"** with an **"Attach file"** control.
- [ ] **3.3** Try uploading a disallowed file type (e.g. `.txt` or `.exe`). Confirm it's rejected — only `.jpg .jpeg .png .heic .heif .pdf` are accepted (file input should also visually restrict to `image/*,.pdf`).
- [ ] **3.4** Upload a valid file (e.g. a `.pdf`) before the due date. Confirm the status becomes **"Submitted"**, and the button now reads **"Replace file"** / **"Replace Submission"**.
- [ ] **3.5** As **Teacher A1**, open the homework detail page's roster. Confirm Student 1 shows Submitted with the correct "Submitted By" (Guardian 1) and a link to the file.
- [ ] **3.6** As **Guardian 2** (also linked to Student 1 via the multi-guardian setup — if you gave Guardian 2 a second child in Class 1, use Student 1 for this step), confirm Guardian 2 can also see and **replace** Student 1's submission — any linked guardian can submit/replace, not just whoever went first.
- [ ] **3.7** Edit the homework's due date to a date in the past (simulating "past due"). As a guardian with no existing submission for a *different* student in the class, confirm their status shows **"Missing"**, and that a first submission is still accepted even though it's late (status becomes **"Late"**, not blocked).
- [ ] **3.8** With the due date now in the past, try to **replace** Student 1's already-Submitted (on-time) submission. Confirm it's blocked with an error like "The due date has passed, so this submission can no longer be replaced."
- [ ] **3.9** Confirm replacing a submission (while still allowed) actually swaps the file — open the file link before and after to confirm it changed, and confirm there's still only one `HomeworkSubmission` row per (homework, student) — no duplicate rows pile up.

---

## 4. Calendar — Holidays & Days Off

- [ ] **4.1** As **Admin A**, open **Calendar** (`core:calendar`). Click **"New Closed Day"**. Add a single-day holiday (start date = end date) and a multi-day range (e.g. a one-week break).
- [ ] **4.2** Confirm the single-day event displays as one date, and the range displays as "M j – M j, Y" (one row, not one row per day).
- [ ] **4.3** Try setting an end date before the start date. Confirm it's rejected with "End date can't be before the start date."
- [ ] **4.4** Edit and then delete one of the events (delete has a confirm dialog). Confirm both actions work and the event disappears from the list.
- [ ] **4.5** As **Teacher A1**, open Calendar. Confirm it's **read-only** (no "New Closed Day", no Edit/Delete) and shows only **upcoming** events (a past event you added shouldn't appear here, only on the admin's full list).
- [ ] **4.6** As **Guardian 1**, confirm the same read-only, upcoming-only view.
- [ ] **4.7** As Teacher A1, try to reach `calendar_new`/`calendar_edit`/`calendar_delete` directly by URL. Confirm 403 on all three — Calendar write access is admin-only, stricter than most other admin+teacher sections.
- [ ] **4.8** Add a closed day matching a homework due date (or fee notice / permission slip deadline) you're about to create. When picking that date on the relevant form, confirm a non-blocking warning appears (something like "the school calendar marks this as a closed day") but the date can still be saved — it's a soft warning, never a hard block.
- [ ] **4.9** Confirm this same soft-warning does **not** appear on a Permission Slip's `event_date` field (only on `response_deadline`).

---

## 5. Guardian ↔ Teacher Messaging

- [ ] **5.1** As **Admin A**, go to **Settings** and turn on messaging ("Messaging (guardian ↔ teacher and class group chat)" checkbox). Save.
- [ ] **5.2** As **Teacher A1** (or Admin A), open Student 1's page and click **"Message"** next to Guardian 1. Confirm a new one-to-one thread opens.
- [ ] **5.3** Send a message as Teacher A1, then log in as Guardian 1 and reply from the **Messages** inbox. Confirm both sides see the conversation and it marks messages as read/seen appropriately.
- [ ] **5.4** As Admin A, turn **off** School A's messaging switch. Confirm:
  - The "Messages" nav item disappears for guardians/teachers.
  - The existing thread (if reached directly by URL/history) still shows all prior messages (nothing is deleted), but the composer is replaced with a read-only notice ("Messaging is currently turned off...").
- [ ] **5.5** Turn messaging back on. Confirm the composer reappears and new messages can be sent again.
- [ ] **5.6** As Guardian 2 (linked to Student 1 and Student 2, both in Class 1, both potentially taught by different teachers if you vary setup), confirm the "Message" button only ever offers teachers actually connected to that specific child's class — never an arbitrary staff directory.
- [ ] **5.7** Try to reach `message_thread_start` with a guardian/teacher/student combination that has no real connection (e.g. Teacher A2 and Student 1, who isn't in Teacher A2's class) by editing the URL. Confirm it's rejected (404) rather than creating a bogus thread.

---

## 6. Class Group Messaging

- [ ] **6.1** With School A messaging on and Class 1's own messaging switch on (default), confirm a **Class Group** thread exists for Class 1 once it has at least one active student — check via the class's own page ("Class Group Chat" link) or the Messages inbox.
- [ ] **6.2** Confirm every guardian linked to an active Class 1 student, plus the homeroom teacher (and any co-teachers), are participants — send a message as Teacher A1 and confirm Guardian 1 and Guardian 2 (if linked to a Class 1 student) both see it.
- [ ] **6.3** Add a new student to Class 1 with a guardian linked. Confirm that guardian is **automatically** added to the class thread's participants without any manual "invite" step.
- [ ] **6.4** Remove a student from Class 1 (archive them or move them to Class 2). Confirm their guardian is automatically dropped from the class thread's participants, but the thread and its message history persist.
- [ ] **6.5** As Teacher A1, toggle **"Switch to announcements only"** on the class thread. Confirm the button label flips to **"Allow guardians to post"**, and that guardians can still read the thread but no longer see a composer.
- [ ] **6.6** Toggle it back off. Confirm guardians can post again.
- [ ] **6.7** As Teacher A1, remove one of your own messages in the class thread (confirm dialog: "Remove this message? It stays visible as removed to the rest of the thread."). Confirm it's replaced with an italic "Message removed by {name}." placeholder rather than disappearing outright, and that removing it again is a harmless no-op.
- [ ] **6.8** Confirm moderation (removing a message) still works even if you temporarily turn School A's messaging switch off — moderation isn't gated on the on/off switch the way composing new messages is.
- [ ] **6.9** As Guardian 1, confirm you cannot toggle "announcements only" or remove another person's message — those controls simply aren't available to a guardian.

---

## 7. Per-School / Per-Class Messaging Switch

- [ ] **7.1** As Admin A, confirm the messaging checkbox lives on **Settings**, with a **"Save Changes"** button and a Cancel link back to the dashboard, and that Settings itself is only reachable by an admin (403 for Teacher A1 and Guardian 1).
- [ ] **7.2** With the school switch on, edit Class 1 and turn its own messaging switch **off** (the per-class opt-out, on by default). Confirm both the one-to-one "Message" buttons for Class 1's teachers/guardians and Class 1's group thread composer become read-only/unavailable, even though the school-level switch is still on.
- [ ] **7.3** Turn Class 1's switch back on. Confirm messaging for that class resumes, without needing to touch the school-level switch.
- [ ] **7.4** Turn the **school**-level switch off while Class 2's class-level switch is on. Confirm Class 2's messaging is still effectively off — the school switch is the master; both must be on for messaging to actually work (`class_messaging_effectively_enabled`).

---

## 8. Fee Notices

- [ ] **8.1** As Admin A, open **Fee Notices** (`core:fees`). Create one for Student 1 with an amount, currency, and due date.
- [ ] **8.2** Confirm it shows status **"Unpaid"** by default and is immediately visible to Guardian 1 (no draft/publish step, unlike Announcements/Report Cards).
- [ ] **8.3** Mark it **Paid** (a separate action/button, not a form field you edit). Confirm the status updates immediately and the amount/due date fields weren't touched.
- [ ] **8.4** Mark a different fee notice **Waived**, then mark it back to **Unpaid**. Confirm all three status transitions work and are reflected for the guardian.
- [ ] **8.5** As **Teacher A1**, confirm you can also create/manage fee notices (Fee Notices is admin+teacher, not admin-only).
- [ ] **8.6** As **Guardian 2** (linked to Student 2, not Student 1), confirm Student 1's fee notice never appears in Guardian 2's view.
- [ ] **8.7** Delete a fee notice as admin. Confirm it's a hard delete.
- [ ] **8.8** As Guardian 1, confirm the fee notice pages/actions for creating or marking paid/waived are not reachable (403).

---

## 9. Permission Slips

- [ ] **9.1** As Admin A, open **Permission Slips**. Create one school-wide (leave class blank) with an event date and a response deadline.
- [ ] **9.2** Create a second one scoped to Class 1 only.
- [ ] **9.3** Open the school-wide slip's detail page. Confirm a response row now exists for every active student at the school with at least one linked guardian (auto-seeded, status **"Pending"**) — including students outside Class 1.
- [ ] **9.4** As Guardian 1, open Permission Slips. Confirm both the school-wide and the Class 1 slip show for Student 1, both as pending.
- [ ] **9.5** Respond **"Yes"** to one and **"No"** to the other, optionally adding notes. Confirm the response is saved with a timestamp and reflected immediately.
- [ ] **9.6** Change your response (yes → no or vice versa). Confirm it updates in place rather than creating a second response row.
- [ ] **9.7** Add a brand-new student (Student 5) to Class 1 with a guardian linked, **after** the Class 1 permission slip already exists. Re-open the slip's detail page (or the guardian's list) and confirm a response row for Student 5 now exists automatically (self-healing sync), without needing to re-save the slip.
- [ ] **9.8** As Guardian 2 (linked to Student 2, not Student 5), try to submit a response for Student 5 by editing the URL. Confirm it's rejected (404/403) — you can only respond for your own linked children.
- [ ] **9.9** Submit an invalid response value (e.g. tamper with the POST to send something other than yes/no). Confirm the error "Choose Yes or No to respond."
- [ ] **9.10** Confirm the response-deadline field shows the closed-day soft warning if it lands on a date you added to the Calendar, but the event-date field does not.
- [ ] **9.11** Delete a permission slip. Confirm its response rows are cascade-deleted too (hard delete).

---

## 10. Attendance Tracking

- [ ] **10.1** As Teacher A1, open **Attendance**. Class 1 should show a "Not yet taken" indicator for today.
- [ ] **10.2** Open Class 1's roster (or "Take Attendance" from the class page). Confirm every active student defaults to **Present**.
- [ ] **10.3** Mark Student 1 **Absent**, Student 2 **Late**, leave the rest Present. Save. Confirm the "Not yet taken" indicator clears afterward.
- [ ] **10.4** Re-take the same day (change Student 1 back to Present) and save again. Confirm there's still exactly one record per student for that day (no duplicates — check Django admin if unsure).
- [ ] **10.5** Navigate to a past date via the roster's prev/next-day links. As Teacher A1, try to change a status there and save. Confirm you're blocked (read-only or a permission error) — only today is freely editable by a teacher.
- [ ] **10.6** As Admin A, open that same past date and confirm you *can* edit and save it (admins can amend older entries).
- [ ] **10.7** Try to navigate to a future date (edit the `?date=` query param). Confirm you're redirected back to today with an error — attendance can't be taken in advance.
- [ ] **10.8** Add a closed day on the Calendar for today. Reopen today's roster and confirm a banner notes the calendar shows it as closed, but you can still record attendance if you want to.
- [ ] **10.9** As Guardian 1, open Student 1's page. Confirm a "Recent Attendance" card shows the entries you made, with a "View all" link to the full history — scoped to Student 1 only.
- [ ] **10.10** As Guardian 1, try to reach the class roster, the Attendance landing page, or the admin overview page by URL. Confirm 403 on all three.
- [ ] **10.11** As Admin A, open the Attendance **School Overview**. Confirm present/absent/late totals per class roughly match what you entered, over about the last 30 days.
- [ ] **10.12** As Teacher A1, confirm the School Overview page is blocked (403) — cross-class totals are admin-only.

---

## 11. Report Cards per Student

- [ ] **11.1** As Admin A, open **Terms**. Add "Term 1" with a date range covering the attendance you entered above.
- [ ] **11.2** Try adding a second term with the same name — confirm it's rejected (unique per school). Try an end date before the start date — confirm it's rejected too.
- [ ] **11.3** As Teacher A1 and Guardian 1, confirm neither can reach Terms (403 on both).
- [ ] **11.4** As Teacher A1, open **Report Cards**. Confirm Class 1 is listed but Class 2 is not (Teacher A1 doesn't teach Class 2).
- [ ] **11.5** Open Class 1's report cards for Term 1 — every active student shows "Not started". Click "Start" on Student 1.
- [ ] **11.6** Confirm the "Attendance This Term" box on the report matches the attendance you recorded for Student 1 within Term 1's dates.
- [ ] **11.7** Fill in two subjects and a comment, click **Save Draft**. Confirm the roster shows Student 1 as **Draft**.
- [ ] **11.8** Re-open Student 1's report. Confirm your subjects/comment are intact, and only a handful of blank rows are added on top (not a large fixed block every time it's reopened).
- [ ] **11.9** Click **"+ Add Subject Row"** a few times — confirm new blank rows appear instantly, no reload. Fill one in, leave another blank, click **Remove** on the blank one — confirm it vanishes instantly.
- [ ] **11.10** Click **Remove** on one of your originally-saved rows too. Save. Confirm no "This field is required" errors pop up for anything you removed or left untouched, and that reopening the report shows the removed rows gone for good (not reappearing).
- [ ] **11.11** Start Student 2's report in the same class/term. Confirm the subject names from Student 1's report are pre-filled, with grades left blank for Student 2 to fill in themselves.
- [ ] **11.12** While Student 1's report is still a Draft, confirm neither Guardian 1 nor Guardian 2 sees it on the child's Report Cards page, and visiting the report's print/view URL directly as a guardian gives a 403.
- [ ] **11.13** Publish Student 1's report. Confirm both Guardian 1 and Guardian 2 (the two guardians linked to Student 1) now see the exact same published report, and that opening "View / Print" shows a clean page with no sidebar/app chrome, the subjects, grades, attendance summary, and comment.
- [ ] **11.14** As Guardian 2, confirm Student 1's report never shows up on Student 2's Report Cards page and vice versa.
- [ ] **11.15** As Guardian 1, try guessing another student's report id in the "view" URL. Confirm 403/404.
- [ ] **11.16** Save drafts (don't publish) for Students 3 and 4, then use **"Publish All Drafts"** on the Class 1/Term 1 roster. Confirm both flip to Published in one action, and already-published reports are unaffected.
- [ ] **11.17** As Teacher A2 (doesn't teach Class 1), try to open Class 1's report-card roster or an individual student's report-edit URL directly. Confirm 404.
- [ ] **11.18** As Admin A, confirm you *can* enter/edit a report for a class you don't personally teach ("on a teacher's behalf").

---

## 12. Multi-Guardian Households

Re-checks the same rule across every feature above: **all** guardians linked to a student see the same thing for that student, and **no** guardian sees anything about a student they're not linked to — even a sibling in the same class.

- [ ] **12.1** Announcements: both Guardian 1 and Guardian 2 (both linked to Student 1) see the same published, class-scoped announcement for Student 1's class, with independent read/unread state per guardian.
- [ ] **12.2** Homework Submission: either guardian can submit or replace Student 1's submission (already checked in 3.6 — re-confirm here as part of the full pass).
- [ ] **12.3** Fee Notices: both guardians see the same fee notice and its status for Student 1.
- [ ] **12.4** Permission Slips: both guardians see the same pending/responded status for Student 1; whichever one responds, the other sees the updated response (not a second, independent response).
- [ ] **12.5** Attendance: both guardians see identical attendance history for Student 1.
- [ ] **12.6** Report Cards: both guardians see the identical published report for Student 1 (already checked in 11.13 — re-confirm here).
- [ ] **12.7** Guardian ↔ Teacher Messaging: confirm whether a one-to-one thread is per (guardian, teacher, student) — i.e. Guardian 1 and Guardian 2 would each have their **own separate** thread with the same teacher about Student 1, not a shared one. Verify this matches expectations (each guardian message-starts independently).
- [ ] **12.8** Class Group Messaging: confirm both Guardian 1 and Guardian 2 appear as participants in Class 1's group thread (since both have a child there) and both can see/post in the same thread.
- [ ] **12.9** Cross-child leakage: confirm nothing in 12.1–12.8 accidentally exposes Student 2's (Guardian 2's other child) data to Guardian 1, who has no connection to Student 2.

---

## 13. Multi-School / Multi-Role

- [ ] **13.1** Create a user who is **Teacher** at School A and also happens to be a **Guardian** at School A for their own child (a staff member whose child attends). Confirm the school switcher / dashboard correctly shows role-appropriate views when switching between an admin-facing and guardian-facing context, if the UI supports both roles for one school.
- [ ] **13.2** Create (or reuse) a user with active memberships at **both School A and School B** (e.g. a guardian with children at two different schools, or reuse Admin A/B distinctly). Confirm the **school switcher** dropdown appears in the top bar and lets you flip between schools.
- [ ] **13.3** While active school = School A, confirm every list (Students, Classes, Announcements, Homework, Fee Notices, Permission Slips, Calendar, Attendance, Report Cards, Terms, Messages) shows **only** School A's data.
- [ ] **13.4** Switch active school to School B. Confirm the same lists now show **only** School B's data — no bleed-through from School A.
- [ ] **13.5** As Admin A, try to reach School B's records by directly editing a URL to School B's class/student/announcement/etc. id. Confirm 404 in every case (not 403) — cross-tenant access fails at the "doesn't exist for you" level, not a permission-denied message.
- [ ] **13.6** As Admin B, confirm the reverse — no access to any of School A's data by guessing ids.
- [ ] **13.7** Log in as a user with **no** SchoolMembership at all (a brand-new account). Confirm the dashboard shows an explicit "not linked to a school" state rather than a confusing empty/zero-count dashboard.
- [ ] **13.8** Confirm role-based nav visibility is consistent for every role at every school: Students/Classes/Guardians/Teachers/Attendance/Report Cards (admin+teacher), Terms/Settings/Calendar-write (admin only), Messages (guardian+teacher, only if messaging is on), and that a guardian never sees any admin/teacher-only nav item, at either school.
- [ ] **13.9** As a **superuser** with no SchoolMembership anywhere, confirm the **"Set Up a School"** nav item is visible, and that a non-superuser (even a school admin) cannot create a brand-new School via that flow (403 if attempted directly).

---

## 14. Regression Check

- [ ] **14.1** Class detail page still shows homeroom teacher, co-teachers, and student roster, with the "Take Attendance" and "Report Cards" entry points sitting alongside the existing "Edit Class" button without visual breakage.
- [ ] **14.2** A guardian's child page (`my_child_detail`) shows Announcements, Homework, Fee Notices, Permission Slips, Attendance, and Report Cards cards all together without any of the older sections breaking or disappearing.
- [ ] **14.3** Django admin (`/admin/`) exposes `Announcement`/`AnnouncementRead`, `Homework`/`HomeworkSubmission`, `SchoolCalendarEvent`, `MessageThread`/`Message`/`MessageThreadRead`, `FeeNotice`, `PermissionSlip`/`PermissionSlipResponse`, `AttendanceRecord`, `Term`, and `ReportCard`/`ReportCardEntry`/`ReportCardRead` — and list/search/filter work on each.
- [ ] **14.4** Log out and confirm every one of these pages redirects an anonymous visitor to the login page rather than showing a 403 or any data.

---

## Sign-off

| Section | Tester | Date | Result (Pass/Fail) | Notes |
|---|---|---|---|---|
| 1. Announcements | | | | |
| 2. Homework | | | | |
| 3. Homework Submission | | | | |
| 4. Calendar — Holidays & Days Off | | | | |
| 5. Guardian ↔ Teacher Messaging | | | | |
| 6. Class Group Messaging | | | | |
| 7. Per-School / Per-Class Messaging Switch | | | | |
| 8. Fee Notices | | | | |
| 9. Permission Slips | | | | |
| 10. Attendance Tracking | | | | |
| 11. Report Cards per Student | | | | |
| 12. Multi-Guardian Households | | | | |
| 13. Multi-School / Multi-Role | | | | |
| 14. Regression Check | | | | |
