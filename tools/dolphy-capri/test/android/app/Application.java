package android.app;
import android.os.Bundle;
public class Application extends android.content.Context {
    public ActivityLifecycleCallbacks callbacks;
    public void registerActivityLifecycleCallbacks(ActivityLifecycleCallbacks callbacks) { this.callbacks = callbacks; }
    public interface ActivityLifecycleCallbacks {
        void onActivityCreated(Activity activity, Bundle saved);
        void onActivityStarted(Activity activity);
        void onActivityResumed(Activity activity);
        void onActivityPaused(Activity activity);
        void onActivityStopped(Activity activity);
        void onActivitySaveInstanceState(Activity activity, Bundle out);
        void onActivityDestroyed(Activity activity);
    }
}
