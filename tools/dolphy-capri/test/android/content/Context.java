package android.content;
import android.content.res.AssetManager;
import android.content.res.Resources;
public class Context {
    public AssetManager getAssets() { return new AssetManager(); }
    public String getPackageName() { return "com.droid.dolphy.capri"; }
    public Resources getResources() { return new Resources(); }
    public void startActivity(Intent intent) {}
}
