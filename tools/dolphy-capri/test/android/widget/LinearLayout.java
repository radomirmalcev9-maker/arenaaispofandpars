package android.widget;
import android.content.Context;
import android.view.ViewGroup;
public class LinearLayout extends ViewGroup {
    public static final int VERTICAL = 1, HORIZONTAL = 0;
    public LinearLayout(Context context) {}
    public void setOrientation(int orientation) {}
    public void setBackgroundColor(int color) {}
    public static class LayoutParams extends ViewGroup.LayoutParams {
        public LayoutParams(int width, int height) { super(width, height); }
        public LayoutParams(int width, int height, float weight) { super(width, height); }
    }
}
