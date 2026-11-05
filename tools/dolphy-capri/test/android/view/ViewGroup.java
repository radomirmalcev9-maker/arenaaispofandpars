package android.view;
public class ViewGroup extends View {
    public static class LayoutParams {
        public static final int MATCH_PARENT = -1;
        public LayoutParams(int width, int height) {}
    }
    public void addView(View view, LayoutParams params) {}
    public void removeView(View view) {}
    public void removeAllViews() {}
    public int getChildCount() { return 1; }
    public View getChildAt(int index) { return new View(); }
    public View findViewWithTag(Object tag) { return null; }
}
