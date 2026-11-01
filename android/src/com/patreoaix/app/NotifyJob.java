package com.patreoaix.app;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.job.JobInfo;
import android.app.job.JobParameters;
import android.app.job.JobScheduler;
import android.app.job.JobService;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Build;
import android.webkit.CookieManager;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;

/**
 * Background check for new account notifications (deposits, trades, P2P orders, alerts, security):
 * every 15 minutes (the shortest Android allows), when the phone restarts, and when the app opens.
 * It uses the app's own login (the site cookie), reads /api/notifications/ and shows each new unread one
 * as an Android notification that opens the right page in the app.
 */
public class NotifyJob extends JobService {
    private static final int PERIODIC_ID = 7001, NOW_ID = 7002;
    private static final String CHANNEL = "alerts";
    private static final String GROUP = "com.patreoaix.app.ALERTS";
    private static final int SUMMARY_ID = 7100;

    static void schedule(Context c) {
        JobScheduler js = (JobScheduler) c.getSystemService(Context.JOB_SCHEDULER_SERVICE);
        if (js == null) return;
        for (JobInfo j : js.getAllPendingJobs()) if (j.getId() == PERIODIC_ID) return;
        JobInfo.Builder b = new JobInfo.Builder(PERIODIC_ID, new ComponentName(c, NotifyJob.class))
            .setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY)
            .setPersisted(true);
        if (Build.VERSION.SDK_INT >= 24) b.setPeriodic(15 * 60 * 1000L, 5 * 60 * 1000L);
        else b.setPeriodic(15 * 60 * 1000L);
        try { js.schedule(b.build()); } catch (Exception e) { /* not allowed on this phone: the app still works */ }
    }

    static void checkNow(Context c) {
        JobScheduler js = (JobScheduler) c.getSystemService(Context.JOB_SCHEDULER_SERVICE);
        if (js == null) return;
        try {
            js.schedule(new JobInfo.Builder(NOW_ID, new ComponentName(c, NotifyJob.class))
                .setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY).setOverrideDeadline(5000).build());
        } catch (Exception e) { }
    }

    @Override
    public boolean onStartJob(final JobParameters params) {
        new Thread(new Runnable() {
            @Override
            public void run() {
                try { check(getApplicationContext()); } catch (Throwable t) { }
                jobFinished(params, false);
            }
        }).start();
        return true;
    }

    @Override
    public boolean onStopJob(JobParameters params) { return true; }

    static void check(Context c) throws Exception {
        String cookie = CookieManager.getInstance().getCookie(MainActivity.BASE);
        if (cookie == null || !cookie.contains("sessionid")) return;                 // not signed in yet
        HttpURLConnection con = (HttpURLConnection) new URL(MainActivity.BASE + "/api/notifications/").openConnection();
        con.setConnectTimeout(15000);
        con.setReadTimeout(15000);
        con.setInstanceFollowRedirects(false);
        con.setRequestProperty("Cookie", cookie);
        con.setRequestProperty("X-Requested-With", "XMLHttpRequest");
        con.setRequestProperty("Accept", "application/json");
        con.setRequestProperty("User-Agent", "PatreoaixApp/1.0 (Android; notifications)");
        if (con.getResponseCode() != 200) return;
        StringBuilder sb = new StringBuilder();
        BufferedReader r = new BufferedReader(new InputStreamReader(con.getInputStream(), "UTF-8"));
        String line;
        while ((line = r.readLine()) != null) sb.append(line);
        r.close();
        JSONObject d = new JSONObject(sb.toString());
        if (!d.optBoolean("success")) return;
        JSONArray items = d.optJSONArray("notifications");
        if (items == null) return;

        SharedPreferences sp = c.getSharedPreferences("notify", Context.MODE_PRIVATE);
        long last = sp.getLong("last_id", -1), max = last;
        boolean first = last < 0;                         // first run: remember what is there, don't flood
        int posted = 0;
        NotificationManager nm = (NotificationManager) c.getSystemService(Context.NOTIFICATION_SERVICE);
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel ch = new NotificationChannel(CHANNEL, c.getString(R.string.channel_name), NotificationManager.IMPORTANCE_HIGH);
            ch.setDescription(c.getString(R.string.channel_desc));
            ch.enableVibration(true);
            ch.setVibrationPattern(new long[]{0, 200, 100, 200});
            ch.enableLights(true);
            ch.setLightColor(0xFFF5B301);
            ch.setShowBadge(true);
            ch.setLockscreenVisibility(Notification.VISIBILITY_PRIVATE);
            nm.createNotificationChannel(ch);
        }
        android.graphics.Bitmap big = null;
        try { big = android.graphics.BitmapFactory.decodeResource(c.getResources(), R.mipmap.ic_launcher); } catch (Exception e) { }
        java.util.ArrayList<String> lines = new java.util.ArrayList<String>();
        for (int k = items.length() - 1; k >= 0; k--) {        // oldest first
            JSONObject n = items.getJSONObject(k);
            long id = n.optLong("id");
            if (id > max) max = id;
            if (first || id <= last || n.optBoolean("is_read")) continue;
            String title = n.optString("title", "PATREOAIX");
            String body = n.optString("message", "");
            String url = n.optString("action_url", "");
            String tag = n.optString("tag", "");
            boolean sticky = n.optBoolean("sticky", false);
            lines.add(title + (body.isEmpty() ? "" : ": " + body.replace('\n', ' ')));
            Intent open = new Intent(c, MainActivity.class).putExtra("url", url.startsWith("/") ? url : "/notifications/")
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
            int flags = PendingIntent.FLAG_UPDATE_CURRENT | (Build.VERSION.SDK_INT >= 23 ? PendingIntent.FLAG_IMMUTABLE : 0);
            PendingIntent pi = PendingIntent.getActivity(c, (int) (id % Integer.MAX_VALUE), open, flags);
            Notification.Builder b = Build.VERSION.SDK_INT >= 26 ? new Notification.Builder(c, CHANNEL) : new Notification.Builder(c);
            b.setSmallIcon(R.drawable.ic_stat)
                .setContentTitle(title.isEmpty() ? "PATREOAIX" : title)
                .setContentText(body)
                .setStyle(new Notification.BigTextStyle().bigText(body))
                .setContentIntent(pi)
                .setAutoCancel(true)
                .setColor(0xFFF5B301)
                .setWhen(System.currentTimeMillis())
                .setShowWhen(true)
                .setGroup(GROUP)                                   // stacked under one PATREOAIX group, like a chat app
                .setCategory(tag.startsWith("p2p-") ? Notification.CATEGORY_MESSAGE : Notification.CATEGORY_EVENT)
                .setVisibility(Notification.VISIBILITY_PRIVATE);
            if (big != null) b.setLargeIcon(big);
            if (sticky) b.setOngoing(false).setPriority(Notification.PRIORITY_MAX);
            if (Build.VERSION.SDK_INT < 26) b.setPriority(Notification.PRIORITY_HIGH).setDefaults(Notification.DEFAULT_ALL);
            try {
                if (!tag.isEmpty()) nm.notify(tag, 1, b.build());       // one notification per P2P order, updated in place
                else nm.notify((int) (id % Integer.MAX_VALUE), b.build());
            } catch (SecurityException e) { return; }
            posted++;
        }
        if (posted > 0 && Build.VERSION.SDK_INT >= 24) {                // the group summary: "3 new alerts"
            Notification.InboxStyle inbox = new Notification.InboxStyle();
            for (int k = Math.max(0, lines.size() - 5); k < lines.size(); k++) inbox.addLine(lines.get(k));
            inbox.setSummaryText(lines.size() + (lines.size() == 1 ? " new alert" : " new alerts"));
            Intent openAll = new Intent(c, MainActivity.class).putExtra("url", "/notifications/")
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
            PendingIntent pAll = PendingIntent.getActivity(c, 0, openAll, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
            Notification.Builder sum = Build.VERSION.SDK_INT >= 26 ? new Notification.Builder(c, CHANNEL) : new Notification.Builder(c);
            sum.setSmallIcon(R.drawable.ic_stat).setColor(0xFFF5B301).setStyle(inbox).setGroup(GROUP).setGroupSummary(true)
                .setContentTitle("PATREOAIX").setContentText(lines.size() + " new alerts").setContentIntent(pAll).setAutoCancel(true);
            if (big != null) sum.setLargeIcon(big);
            try { nm.notify(SUMMARY_ID, sum.build()); } catch (SecurityException e) { }
        }
        sp.edit().putLong("last_id", Math.max(max, 0)).apply();
    }
}
