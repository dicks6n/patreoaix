package com.patreoaix.app;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** after a restart or an update: keep the notification checks running */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context c, Intent i) { NotifyJob.schedule(c); }
}
