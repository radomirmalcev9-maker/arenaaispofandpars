package app.capri.integration;

import android.app.Application;
import android.content.ContentProvider;
import android.content.ContentValues;
import android.database.Cursor;
import android.net.Uri;

/** Installs the tab/lifecycle bridge even when Dolphy is opened from NFC or a widget. */
public final class BootstrapProvider extends ContentProvider {
    @Override public boolean onCreate() {
        if (getContext() != null && getContext().getApplicationContext() instanceof Application) {
            Tabs.install((Application) getContext().getApplicationContext());
        }
        return true;
    }
    @Override public Cursor query(Uri uri, String[] projection, String selection, String[] args, String order) { return null; }
    @Override public String getType(Uri uri) { return null; }
    @Override public Uri insert(Uri uri, ContentValues values) { return null; }
    @Override public int delete(Uri uri, String selection, String[] args) { return 0; }
    @Override public int update(Uri uri, ContentValues values, String selection, String[] args) { return 0; }
}
