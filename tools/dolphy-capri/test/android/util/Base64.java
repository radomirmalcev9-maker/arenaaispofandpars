package android.util;
public class Base64 {
    public static final int DEFAULT = 0, NO_WRAP = 2;
    public static byte[] decode(String value, int flags) { return java.util.Base64.getDecoder().decode(value); }
    public static String encodeToString(byte[] value, int flags) { return java.util.Base64.getEncoder().encodeToString(value); }
}
