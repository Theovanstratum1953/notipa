# Manual Test Plan — v1.1.0 (Attendance Tracking & Report Cards per Student)

Covers the two features added in this release: **Attendance Tracking** and **Report Cards per Student**. Each test case lists the steps to follow, the role to be logged in as, and the expected result. Check off each box as you go; anything that doesn't match the expected result is a bug.

Run through this on a fresh test school where you're comfortable creating throwaway data (students, classes, terms, report cards).

## 0. Setup

Do these once, before running the test cases below.

- [ ] **0.1** As a superuser, create a test school (`Set Up a School`), or use an existing test school.
- [ ] **0.2** As the school's admin, create two teachers: **Teacher A** and **Teacher B**.
- [ ] **0.3** Create two classes: **Class 1** (homeroom teacher = Teacher A) and **Class 2** (homeroom teacher = Teacher B). Teacher A should *not* be a co-teacher on Class 2, and vice versa.
- [ ] **0.4** Create four students in Class 1: **Student 1**, **Student 2**, **Student 3**, **Student 4**. All active.
- [ ] **0.5** Create two guardian accounts: **Guardian 1** and **Guardian 2**.
  - Link **Guardian 1** to **Student 1** only.
  - Link **Guardian 2** to **Student 1** *and* Student 2 (so Student 1 has two guardians — a multi-guardian household — and Guardian 2 also has a second, unrelated child to check cross-child scoping).
- [ ] **0.6** Note today's date and have at least one past date (a few days ago) in mind for date-based tests below.

---

## 1. Attendance Tracking

### 1.1 Taking the daily roster

- [ ] **1.1.1** Log in as **Teacher A**. Open **Attendance** from the sidebar. Class 1 should be listed with a "Not yet taken" tag for today.
- [ ] **1.1.2** Click into Class 1's roster (or "Take Attendance" from the class's own page). Every active student should default to **Present**.
- [ ] **1.1.3** Mark Student 1 **Absent** and Student 2 **Late**, leave Student 3 and Student 4 as **Present**. Save.
- [ ] **1.1.4** You're redirected back to the same date's roster. Confirm the statuses you just set are shown correctly (Absent/Late/Present/Present).
- [ ] **1.1.5** Go back to the Attendance list or the class's own page — the "Not yet taken" tag should now read **Taken** (or the tag should be gone).
- [ ] **1.1.6** Re-open the same day's roster and change Student 1 from Absent to Present, then save again. Confirm there's still only one record per student for that day (no duplicates) — check the Django admin for `AttendanceRecord` if you want to confirm directly.

### 1.2 Date navigation & the edit window

- [ ] **1.2.1** On the roster page, use the "previous day" link to go back a few days. Confirm the page shows (correctly) that nothing was taken for that day (all blank/present, no "Taken" tag) unless you've entered something for it.
- [ ] **1.2.2** While viewing a **past date** (not today) as **Teacher A**, try to change a status and save. You should be blocked — either the controls are disabled/read-only, or submitting shows a permission error. A note should explain that only an admin can amend older entries.
- [ ] **1.2.3** Log in as the **admin** and open that same past date's roster for Class 1. Confirm you *can* change and save a status for that older date.
- [ ] **1.2.4** Try to navigate to a **future date** (edit the `?date=` in the URL to tomorrow, or use "next day" if visible). Confirm you're redirected back to today with an error message — you shouldn't be able to take attendance in advance.

### 1.3 School calendar / closed-day awareness

- [ ] **1.3.1** As admin, add a closed day (e.g. a holiday) on the school Calendar for today's date (or a date you can navigate to).
- [ ] **1.3.2** Open that date's attendance roster. Confirm a banner appears noting the school calendar shows this as a closed day, and that you can still take attendance if you choose to (it's a note, not a block).

### 1.4 Guardian visibility

- [ ] **1.4.1** Log in as **Guardian 1**. From the dashboard, open Student 1's page. Confirm there's a "Recent Attendance" section showing the record(s) you entered, with a "View all" link.
- [ ] **1.4.2** Click "View all" — confirm the full attendance history page loads and shows only Student 1's records.
- [ ] **1.4.3** Try to directly visit an attendance roster URL for Class 1 (e.g. `/attendance/classes/<class-id>/`) while logged in as Guardian 1. Confirm you get a permission-denied (403) page, not the roster.
- [ ] **1.4.4** Try visiting the Attendance list page and the admin overview page as Guardian 1 — both should be blocked (403).
- [ ] **1.4.5** Log in as **Guardian 2** and open Student 2's attendance page. Confirm you can see Student 2's own records but nothing about Student 1 (even though Guardian 2 can also see Student 1 elsewhere) — i.e. each child's attendance page only ever shows that one child.

### 1.5 Admin overview

- [ ] **1.5.1** Log in as admin and open the Attendance **School Overview** page. Confirm it shows present/absent/late totals per class for roughly the last 30 days, and that the numbers match what you entered above.
- [ ] **1.5.2** Log in as **Teacher A** and confirm the School Overview page is blocked (403) — only admins see the cross-class view.

---

## 2. Report Cards per Student

### 2.1 Terms

- [ ] **2.1.1** Log in as admin and open **Terms** (Admin section of the sidebar). Add a term, e.g. "Term 1", with a start date a few weeks in the past and an end date a few weeks in the future (so it covers the attendance you entered above).
- [ ] **2.1.2** Try adding a second term with the *same name* — confirm it's rejected (name must be unique per school).
- [ ] **2.1.3** Edit the term's dates; confirm an end date before the start date is rejected with a clear error.
- [ ] **2.1.4** Log in as **Teacher A** and as **Guardian 1**; confirm neither can reach `Terms`/`New Term` (403 on both).

### 2.2 Entering a report card

- [ ] **2.2.1** Log in as **Teacher A**. Open **Report Cards** from the sidebar — Class 1 should be listed (Class 2 should not, since Teacher A doesn't teach it).
- [ ] **2.2.2** Open Class 1's report cards for Term 1. Every active student should show as "Not started".
- [ ] **2.2.3** Click "Start" on Student 1. Confirm:
  - The "Attendance This Term" box shows counts that match what you entered in section 1 for Student 1 within Term 1's date range.
  - A handful of blank subject rows are shown (a new report with nothing entered yet should offer a generous but not excessive number of starter rows).
- [ ] **2.2.4** Fill in two subjects (e.g. "Mathematics" / "A", "Science" / "B+") and a comment. Click **Save Draft**.
- [ ] **2.2.5** Confirm you're returned to the roster and Student 1 now shows status **Draft**.
- [ ] **2.2.6** Re-open Student 1's report. Confirm your two subjects and comment are still there, and that only a few blank rows are added beyond your saved ones (not a large fixed block of empties every time you reopen it).

### 2.3 Adding and removing subject rows

- [ ] **2.3.1** On a report's edit page, click **"+ Add Subject Row"** several times. Confirm new blank rows appear immediately without the page reloading.
- [ ] **2.3.2** Fill in one of the newly added rows, leave another one blank, then click **Remove** on the blank one. Confirm it disappears immediately.
- [ ] **2.3.3** Click **Remove** on one of your originally-saved, filled-in rows (e.g. "Science"). Confirm it disappears immediately.
- [ ] **2.3.4** Click **Save Draft**. Confirm no "This field is required" errors appear for any of the rows you removed or left untouched.
- [ ] **2.3.5** Re-open the report. Confirm the row you removed in 2.3.3 (Science) is gone for good, the row you filled and kept is present, and the blank row you removed in 2.3.2 has not reappeared.

### 2.4 Subject list copied across students

- [ ] **2.4.1** Still as Teacher A, go back to the Class 1 / Term 1 roster and click "Start" on **Student 2** (a student with no report yet for this term).
- [ ] **2.4.2** Confirm the subject names from Student 1's report (e.g. "Mathematics") appear pre-filled, but with the **grade left blank** — you're expected to fill in Student 2's own grade, not inherit Student 1's.
- [ ] **2.4.3** Fill in grades and save as a draft.

### 2.5 Draft vs. published visibility

- [ ] **2.5.1** With Student 1's report still in **Draft**, log in as **Guardian 1** and **Guardian 2** in turn. Open Student 1's "Report Cards" — confirm neither guardian sees Term 1 listed.
- [ ] **2.5.2** Try to directly visit the report's "view" URL as a guardian while it's still a draft. Confirm you get a 403.
- [ ] **2.5.3** Log back in as Teacher A, open Student 1's report, and click **Publish**. Confirm the roster now shows Student 1 as **Published**.
- [ ] **2.5.4** Log in as **Guardian 1**: confirm Term 1 now appears on Student 1's Report Cards page, and clicking "View / Print" opens a clean, print-friendly page (no sidebar/app chrome) showing the subjects, grades, attendance summary, and comment.
- [ ] **2.5.5** Log in as **Guardian 2** (the second guardian linked to Student 1): confirm they see the *exact same* published report — multi-guardian households both get access.
- [ ] **2.5.6** As Guardian 2, open Student 2's Report Cards page — confirm Student 1's report never appears there and vice versa.
- [ ] **2.5.7** As **Guardian 1**, try to view Student 2's or Student 3's report by guessing/editing the report's URL. Confirm you get a 403/404 rather than seeing another family's child's report.

### 2.6 Bulk publish

- [ ] **2.6.1** As Teacher A, save drafts (don't publish) for Student 3 and Student 4 in Term 1.
- [ ] **2.6.2** From the Class 1 / Term 1 roster page, click **"Publish All Drafts"**. Confirm you're asked to confirm, and afterward Student 3 and Student 4 both show as **Published** (and Student 1/2, already published, are unaffected).
- [ ] **2.6.3** Confirm the drafts-remaining count / button updates or disables once there are no more drafts to publish.

### 2.7 Teacher scoping

- [ ] **2.7.1** Log in as **Teacher B** (who teaches Class 2, not Class 1). Try to open Class 1's report card roster directly by URL. Confirm you get a 404 — Teacher B has no access to a class they don't teach.
- [ ] **2.7.2** Log in as **admin** and open Class 2's (or Class 1's) report cards even though the admin doesn't teach either — confirm admins can enter/edit reports for any class ("on a teacher's behalf").

### 2.8 Cross-school scoping (if you have a second test school)

- [ ] **2.8.1** As the second school's admin, confirm you cannot open the first school's Terms, classes, or report cards by guessing/editing IDs in the URL (404 in every case).

---

## 3. Regression check

- [ ] **3.1** Confirm the class detail page still shows the homeroom teacher, co-teachers, and student roster correctly, with the new "Take Attendance" button and "Report Cards" entry point both visible and working alongside the existing "Edit Class" button.
- [ ] **3.2** Confirm a guardian's child page (`my_child_detail`) still shows Announcements, Homework, Fee Notices, and Permission Slips correctly alongside the new Attendance and Report Cards cards — nothing from the older sections should have broken or moved unexpectedly.
- [ ] **3.3** Confirm the Django admin (`/admin/`) shows `Term`, `ReportCard` (with its subject-row and read-tracking inlines), and `AttendanceRecord`, and that list/search/filter work on each.

---

## Sign-off

| Section | Tester | Date | Result (Pass/Fail) | Notes |
|---|---|---|---|---|
| 1. Attendance Tracking | | | | |
| 2. Report Cards per Student | | | | |
| 3. Regression check | | | | |
