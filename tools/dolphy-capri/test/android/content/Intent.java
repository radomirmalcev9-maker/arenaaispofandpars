package android.content;
public class Intent {
    public static final int FLAG_ACTIVITY_REORDER_TO_FRONT = 1, FLAG_ACTIVITY_SINGLE_TOP = 2, FLAG_ACTIVITY_NEW_TASK = 4;
    public Intent setClassName(Context context, String name) { return this; }
    public Intent addFlags(int flags) { return this; }
    public Intent putExtra(String key, String value) { return this; }
}
