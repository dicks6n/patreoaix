package com.patreoaix.app;

import android.Manifest;
import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.CookieManager;
import android.webkit.DownloadListener;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;
import android.widget.ProgressBar;

/**
 * PATREOAIX for Android: the website full screen.
 *  - links to the site stay in the app; other sites, phone, email and WhatsApp links open in their own apps
 *  - file upload (KYC documents, profile photo, chart screenshots, P2P payment proof)
 *  - the phone's back button goes back in the site, then leaves the app
 *  - a friendly page when there is no internet, with a retry button
 *  - new account notifications become Android notifications (NotifyJob, every 15 minutes and on opening)
 */
public class MainActivity extends Activity {
    static final String BASE = "https://www.patreoaix.com";
    static final String START = BASE + "/?source=app";
    private static final int FILE_REQ = 41, PERM_REQ = 42;

    private WebView web;
    private ProgressBar bar;
    private ValueCallback<Uri[]> fileCallback;
    private String failedUrl;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.parseColor("#0B0D12"));
        root.setFitsSystemWindows(true);

        web = new WebView(this);
        web.setBackgroundColor(Color.parseColor("#0B0D12"));
        root.addView(web, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        bar = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        bar.setMax(100);
        bar.setIndeterminate(false);
        FrameLayout.LayoutParams lp = new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(3));
        root.addView(bar, lp);
        if (Build.VERSION.SDK_INT >= 21) bar.setProgressTintList(android.content.res.ColorStateList.valueOf(Color.parseColor("#F5B301")));
        setContentView(root);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setSupportZoom(false);
        s.setBuiltInZoomControls(false);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        s.setAllowFileAccess(false);
        s.setUserAgentString(s.getUserAgentString() + " PatreoaixApp/" + appVersion() + " (Android)");

        CookieManager cm = CookieManager.getInstance();
        cm.setAcceptCookie(true);
        cm.setAcceptThirdPartyCookies(web, true);

        web.setWebViewClient(new Client());
        web.setWebChromeClient(new Chrome());
        web.setDownloadListener(new DownloadListener() {
            @Override
            public void onDownloadStart(String url, String ua, String cd, String mime, long len) { openOutside(url); }
        });

        if (state != null) web.restoreState(state);
        if (state == null || web.getUrl() == null) web.loadUrl(urlFrom(getIntent()));

        askNotificationPermission();
        NotifyJob.schedule(this);
        NotifyJob.checkNow(this);
    }

    private int dp(int v) { return (int) (v * getResources().getDisplayMetrics().density + 0.5f); }

    private String appVersion() {
        try { return getPackageManager().getPackageInfo(getPackageName(), 0).versionName; } catch (Exception e) { return "1.0"; }
    }

    /** where to go when opened from a notification or a link */
    private String urlFrom(Intent i) {
        if (i != null) {
            String u = i.getStringExtra("url");
            if (u != null && u.startsWith("/")) return BASE + u;
            Uri d = i.getData();
            if (d != null && ours(d)) return d.toString();
        }
        return START;
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        String u = urlFrom(intent);
        if (!START.equals(u)) web.loadUrl(u);
    }

    static boolean ours(Uri u) {
        String h = u.getHost();
        return "https".equals(u.getScheme()) && h != null && (h.equals("patreoaix.com") || h.endsWith(".patreoaix.com"));
    }

    private void openOutside(String url) {
        try {
            Intent i = url.startsWith("intent:") ? Intent.parseUri(url, Intent.URI_INTENT_SCHEME) : new Intent(Intent.ACTION_VIEW, Uri.parse(url));
            i.addCategory(Intent.CATEGORY_BROWSABLE);
            startActivity(i);
        } catch (Exception e) {
            // nothing can open it: ignore
        }
    }

    private class Client extends WebViewClient {
        private boolean route(String url) {
            Uri u = Uri.parse(url);
            String h = u.getHost() == null ? "" : u.getHost();
            if (ours(u) || "about".equals(u.getScheme()) || "data".equals(u.getScheme())) return false;   // stays in the app
            if ("https".equals(u.getScheme()) && (h.equals("oauth.deriv.com") || h.endsWith(".deriv.com") && h.startsWith("oauth"))) return false;   // Deriv login returns to the app
            openOutside(url);
            return true;
        }

        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest req) { return route(req.getUrl().toString()); }

        @Override
        @SuppressWarnings("deprecation")
        public boolean shouldOverrideUrlLoading(WebView view, String url) { return route(url); }

        @Override
        public void onPageStarted(WebView view, String url, Bitmap icon) { bar.setVisibility(View.VISIBLE); }

        @Override
        public void onPageFinished(WebView view, String url) {
            bar.setVisibility(View.GONE);
            CookieManager.getInstance().flush();              // keep the login for the notification checks
        }

        @Override
        public void onReceivedError(WebView view, WebResourceRequest req, WebResourceError err) {
            if (req.isForMainFrame()) offline(view, req.getUrl().toString());
        }

        @Override
        @SuppressWarnings("deprecation")
        public void onReceivedError(WebView view, int code, String desc, String url) {
            if (Build.VERSION.SDK_INT < 23) offline(view, url);
        }
    }

    private void offline(WebView view, String url) {
        failedUrl = url;
        String safe = url == null ? START : url.replace("'", "%27").replace("\"", "%22").replace("<", "%3C");
        String html = "<!doctype html><html><head><meta name=viewport content='width=device-width,initial-scale=1'>"
            + "<style>body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;background:#0b0d12;color:#e8ecf3;"
            + "font-family:-apple-system,Roboto,sans-serif;text-align:center;padding:24px;box-sizing:border-box}"
            + "b{display:block;font-size:20px;margin:14px 0 6px}p{color:#8b93a7;font-size:14px;line-height:1.5;margin:0 0 20px}"
            + "a{display:inline-block;background:#f5b301;color:#0b0e14;font-weight:800;padding:13px 26px;border-radius:12px;text-decoration:none}"
            + ".i{width:64px;height:64px;border-radius:18px;background:#f5b301;display:inline-flex;align-items:center;justify-content:center;font-size:30px}</style></head>"
            + "<body><div><div class=i>&#128225;</div><b>No internet connection</b><p>Check your mobile data or Wi-Fi, then try again.</p>"
            + "<a href='" + safe + "'>Try again</a></div></body></html>";
        view.loadDataWithBaseURL(BASE, html, "text/html", "utf-8", url);
    }

    private class Chrome extends WebChromeClient {
        @Override
        public void onProgressChanged(WebView view, int p) { bar.setProgress(p); bar.setVisibility(p < 100 ? View.VISIBLE : View.GONE); }

        @Override
        public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> cb, FileChooserParams params) {
            if (fileCallback != null) fileCallback.onReceiveValue(null);
            fileCallback = cb;
            try {
                Intent i = params.createIntent();
                i.addCategory(Intent.CATEGORY_OPENABLE);
                if (params.getMode() == FileChooserParams.MODE_OPEN_MULTIPLE) i.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
                startActivityForResult(Intent.createChooser(i, "Choose a file"), FILE_REQ);
                return true;
            } catch (ActivityNotFoundException e) {
                fileCallback = null;
                return false;
            }
        }
    }

    @Override
    protected void onActivityResult(int req, int res, Intent data) {
        if (req == FILE_REQ && fileCallback != null) {
            Uri[] result = WebChromeClient.FileChooserParams.parseResult(res, data);
            if (result == null && res == RESULT_OK && data != null && data.getClipData() != null) {
                int n = data.getClipData().getItemCount();
                result = new Uri[n];
                for (int k = 0; k < n; k++) result[k] = data.getClipData().getItemAt(k).getUri();
            }
            fileCallback.onReceiveValue(result);
            fileCallback = null;
            return;
        }
        super.onActivityResult(req, res, data);
    }

    @Override
    @SuppressWarnings("deprecation")
    public void onBackPressed() {
        if (web.canGoBack()) web.goBack();
        else super.onBackPressed();
    }

    private void askNotificationPermission() {
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, PERM_REQ);
        }
    }

    @Override
    protected void onSaveInstanceState(Bundle out) {
        super.onSaveInstanceState(out);
        web.saveState(out);
    }

    @Override
    protected void onResume() { super.onResume(); web.onResume(); }

    @Override
    protected void onPause() { web.onPause(); CookieManager.getInstance().flush(); super.onPause(); }

    @Override
    protected void onDestroy() {
        if (web != null) { ((ViewGroup) web.getParent()).removeView(web); web.destroy(); }
        super.onDestroy();
    }
}
