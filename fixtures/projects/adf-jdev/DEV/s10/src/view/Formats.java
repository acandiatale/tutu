package view;

/** Plain utility — no ADF import. */
public final class Formats {
    private Formats() {
    }

    public static String pad(String s) {
        return s == null ? "" : s.trim();
    }
}
