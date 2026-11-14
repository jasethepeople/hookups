package com.jasethepeople.hookups;

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Bundle;
import android.view.Gravity;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import androidx.browser.customtabs.CustomTabsIntent;

public final class MainActivity extends Activity {
    private static final int BACKGROUND = Color.rgb(14, 20, 30);
    private static final int PANEL = Color.rgb(24, 34, 49);
    private static final int TEXT = Color.rgb(244, 247, 250);
    private static final int MUTED = Color.rgb(169, 181, 195);
    private static final int ACCENT = Color.rgb(112, 225, 193);

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().setStatusBarColor(BACKGROUND);
        getWindow().setNavigationBarColor(BACKGROUND);
        setContentView(createContent());
    }

    private LinearLayout createContent() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setGravity(Gravity.CENTER_VERTICAL);
        root.setPadding(dp(28), dp(32), dp(28), dp(32));
        root.setBackgroundColor(BACKGROUND);

        TextView eyebrow = text("PRIVATE DASHBOARD", 12, ACCENT, Typeface.BOLD);
        root.addView(eyebrow, new LinearLayout.LayoutParams(-1, -2));

        TextView title = text("Hookups", 34, TEXT, Typeface.BOLD);
        LinearLayout.LayoutParams titleParams = new LinearLayout.LayoutParams(-1, -2);
        titleParams.topMargin = dp(10);
        root.addView(title, titleParams);

        TextView intro = text(
                "Open your hosted dashboard with Zo sign-in. Chrome handles authentication; this APK stores no password or listing data.",
                16,
                MUTED,
                Typeface.NORMAL);
        LinearLayout.LayoutParams introParams = new LinearLayout.LayoutParams(-1, -2);
        introParams.topMargin = dp(12);
        root.addView(intro, introParams);

        LinearLayout notice = new LinearLayout(this);
        notice.setOrientation(LinearLayout.VERTICAL);
        notice.setPadding(dp(18), dp(18), dp(18), dp(18));
        GradientDrawable panel = new GradientDrawable();
        panel.setColor(PANEL);
        panel.setCornerRadius(dp(16));
        notice.setBackground(panel);
        TextView noticeTitle = text("Current dashboard", 15, TEXT, Typeface.BOLD);
        notice.addView(noticeTitle, new LinearLayout.LayoutParams(-1, -2));
        TextView noticeBody = text(
                "The hosted instance currently contains 10 synthetic sample listings. Collection and scheduling remain on your Zo computer.",
                14,
                MUTED,
                Typeface.NORMAL);
        LinearLayout.LayoutParams noticeBodyParams = new LinearLayout.LayoutParams(-1, -2);
        noticeBodyParams.topMargin = dp(8);
        notice.addView(noticeBody, noticeBodyParams);
        LinearLayout.LayoutParams noticeParams = new LinearLayout.LayoutParams(-1, -2);
        noticeParams.topMargin = dp(28);
        root.addView(notice, noticeParams);

        Button open = new Button(this);
        open.setText("Open dashboard");
        open.setTextColor(BACKGROUND);
        open.setAllCaps(false);
        open.setTextSize(16);
        open.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        open.setBackgroundTintList(android.content.res.ColorStateList.valueOf(ACCENT));
        open.setOnClickListener(view -> openDashboard());
        LinearLayout.LayoutParams buttonParams = new LinearLayout.LayoutParams(-1, dp(54));
        buttonParams.topMargin = dp(22);
        root.addView(open, buttonParams);

        return root;
    }

    private TextView text(String value, int sizeSp, int color, int style) {
        TextView view = new TextView(this);
        view.setText(value);
        view.setTextColor(color);
        view.setTextSize(sizeSp);
        view.setTypeface(Typeface.DEFAULT, style);
        return view;
    }

    private void openDashboard() {
        Uri uri = Uri.parse(getString(R.string.dashboard_url));
        try {
            new CustomTabsIntent.Builder().setShowTitle(true).build().launchUrl(this, uri);
        } catch (ActivityNotFoundException exception) {
            try {
                startActivity(new Intent(Intent.ACTION_VIEW, uri));
            } catch (ActivityNotFoundException noBrowser) {
                Toast.makeText(this, "No browser is installed.", Toast.LENGTH_LONG).show();
            }
        }
    }

    private int dp(int value) {
        return (int) (value * getResources().getDisplayMetrics().density + 0.5f);
    }
}
