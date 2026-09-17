package com.ofdrwconverter.cli;

import org.ofdrw.core.basicStructure.pageObj.Content;
import org.ofdrw.core.basicStructure.pageObj.layer.CT_Layer;
import org.ofdrw.core.basicStructure.pageObj.layer.PageBlockType;
import org.ofdrw.core.basicStructure.pageObj.layer.block.CT_PageBlock;
import org.ofdrw.core.basicStructure.pageObj.layer.block.TextObject;
import org.ofdrw.core.basicType.ST_Array;
import org.ofdrw.core.basicType.ST_Box;
import org.ofdrw.core.text.TextCode;
import org.ofdrw.reader.OFDReader;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;

/**
 * 按版式几何还原阅读顺序的文本抽取器（导出纯文本的默认实现）。
 *
 * <p>ofdrw 自带的 {@code TextExporter} 只是把每个 {@code TextCode} 的文本原样
 * 打印成一行（见其依赖的 {@code ContentExtractor}），切行完全不参考坐标。
 * 而实际 OFD 生产中大量存在「一个字（或一两个词）一个 TextObject」的排版方式，
 * 于是导出结果会被打散成大量只含一两个字符的短行，例如：</p>
 *
 * <pre>
 *   1
 *   -
 *   202
 *   6
 * </pre>
 *
 * <p>本类改为按坐标重建版面：先把每个文字对象展开成逐个字符的图元（glyph），
 * 再用字符自身的坐标做行聚类、行内按 x 排序、按字间距决定是否补空格。
 * 依据（均已在真实样例上核对）：</p>
 *
 * <ul>
 *   <li>{@code TextObject.Boundary} 是**页面坐标系**下的外接矩形（毫米）；</li>
 *   <li>{@code TextCode.X/Y} 是相对该对象原点的偏移，{@code Y} 可视为基线偏移；</li>
 *   <li>{@code TextCode.DeltaX/DeltaY} 是逐字的步进量，其累加值等于文本实际宽度
 *       （因此既能还原每个字的横坐标，也能识别对象内部的换行）；</li>
 *   <li>{@code CTM} 非单位矩阵时坐标系发生旋转/缩放，此时不做逐字展开，退化为
 *       「整个对象按 Boundary 当一个图元」处理，避免算出错误的字符坐标。</li>
 * </ul>
 *
 * <p>已知局限：多栏排版（如双栏论文）仍按 y 逐行输出，栏与栏的内容会被交替
 * 合并；竖排文字按行输出为单字行。这类文档建议用 CLI 的 {@code --mode raw}
 * 与默认输出对照后人工取舍。</p>
 *
 * @author ofdrw-converter plugin
 */
final class LayoutTextExtractor {

    /** 行聚类容差：对象视觉高度的比例。中文字号与行距通常相差 1 倍以上，取值偏小可避免误合并相邻行。 */
    private static final double HEIGHT_TOLERANCE_RATIO = 0.35;
    /** 行聚类容差的绝对下限（毫米），避免极小字号时容差退化为 0。 */
    private static final double MIN_HEIGHT_TOLERANCE_MM = 0.4;
    /** 行内补空格阈值：相邻字符间隙超过「较大字高 × 该比例」时补一个空格。 */
    private static final double SPACE_GAP_RATIO = 0.3;

    private LayoutTextExtractor() {
    }

    /** 抽取结果。 */
    static final class Result {
        /** 还原出的文本行（已按阅读顺序排序）。 */
        final List<String> lines;
        /** 展开出的字符图元总数。 */
        final int glyphCount;
        /** 参与排版的文字对象数。 */
        final int objectCount;
        /** 因缺少坐标信息（无 Boundary / 旋转 CTM）而按整块处理的对象数。 */
        final int fallbackObjectCount;

        Result(List<String> lines, int glyphCount, int objectCount, int fallbackObjectCount) {
            this.lines = lines;
            this.glyphCount = glyphCount;
            this.objectCount = objectCount;
            this.fallbackObjectCount = fallbackObjectCount;
        }
    }

    /**
     * 抽取指定页面的文本行。
     *
     * @param reader  OFD 解析器
     * @param pageNum 页码，从 1 开始
     * @return 抽取结果；页面无内容时返回空行列表
     */
    static Result extract(OFDReader reader, int pageNum) {
        List<TextObject> objects = new ArrayList<>();
        Content content = reader.getPage(pageNum).getContent();
        if (content != null) {
            for (CT_Layer layer : content.getLayers()) {
                collect(layer.getPageBlocks(), objects);
            }
        }

        List<Glyph> glyphs = new ArrayList<>();
        int fallback = 0;
        for (int i = 0; i < objects.size(); i++) {
            if (!expand(objects.get(i), i, glyphs)) {
                fallback++;
            }
        }
        if (glyphs.isEmpty()) {
            return new Result(new ArrayList<>(), 0, objects.size(), fallback);
        }
        return new Result(buildLines(glyphs), glyphs.size(), objects.size(), fallback);
    }

    /** 递归收集页面内所有文字对象（保持文档顺序）。 */
    private static void collect(List<PageBlockType> blocks, List<TextObject> out) {
        for (PageBlockType block : blocks) {
            if (block instanceof TextObject) {
                out.add((TextObject) block);
            } else if (block instanceof CT_PageBlock) {
                collect(((CT_PageBlock) block).getPageBlocks(), out);
            }
        }
    }

    /** 参与排版的原子单元：一段可定位的文本。 */
    private static final class Glyph {
        String text;
        double x;
        double y;
        /** 该单元自身的横向步进（用于计算与后继单元的间隙）。 */
        double advance;
        /** 视觉高度，用于行聚类容差与补空格判定。 */
        double height;
        /** 文档顺序，作为坐标相同时的稳定排序依据。 */
        int order;
    }

    /**
     * 把一个文字对象展开成图元并追加到 {@code out}。
     *
     * @return true 表示成功按坐标定位；false 表示缺少坐标信息、已退化为整块处理
     */
    private static boolean expand(TextObject object, int order, List<Glyph> out) {
        ST_Box boundary = object.getBoundary();
        double bx = num(boundary == null ? null : boundary.getTopLeftX(), 0);
        double by = num(boundary == null ? null : boundary.getTopLeftY(), 0);
        double bw = num(boundary == null ? null : boundary.getWidth(), 0);
        double bh = num(boundary == null ? null : boundary.getHeight(), 0);
        double size = num(object.getSize(), 0);
        // 部分 OFD 的 Size 会是畸高值（例如 209mm，实际靠 CTM 缩放），
        // 所以优先用 Boundary 高度作为「字号」的视觉代理。
        double height = bh > 0 ? bh : (size > 0 ? size : 0);

        List<TextCode> codes = object.getTextCodes();
        if (codes == null || codes.isEmpty()) {
            return true;
        }

        boolean positioned = boundary != null && bw > 0 && bh > 0 && isIdentityCtm(object.getCTM());
        for (TextCode code : codes) {
            if (code.getX() == null || code.getY() == null) {
                positioned = false;
                break;
            }
        }

        if (!positioned) {
            // 坐标系被旋转/缩放，或缺少 Boundary：把整个对象的文本当作不可再分的
            // 一块，按 Boundary 定位。这样至少能保证它在页面上排到正确位置，
            // 且不会把不该合并/拆分的字符弄错。
            String text = joinContent(codes);
            if (text.isEmpty()) {
                return true;
            }
            Glyph g = new Glyph();
            g.text = text;
            g.x = bx;
            g.y = by + bh / 2.0;
            g.advance = bw > 0 ? bw : Math.max(height, 1.0) * text.length();
            g.height = height;
            g.order = order;
            out.add(g);
            return false;
        }

        for (TextCode code : codes) {
            String text = code.getContent();
            if (text == null || text.isEmpty()) {
                continue;
            }
            double x = bx + num(code.getX(), 0);
            double y = by + num(code.getY(), 0);
            double[] dx = toDoubles(code.getDeltaX());
            double[] dy = toDoubles(code.getDeltaY());
            // DeltaX 缺失时按「对象可用宽度均分」估算，保证后续字符坐标仍可用。
            double defaultStep = dx.length == 0
                    ? (bw - num(code.getX(), 0)) / Math.max(text.length(), 1)
                    : dx[0];
            if (!(defaultStep > 0)) {
                defaultStep = Math.max(height, 1.0);
            }
            for (int i = 0; i < text.length(); i++) {
                char ch = text.charAt(i);
                double step = pick(dx, i, defaultStep);
                if (ch != '\n' && ch != '\r') {
                    Glyph g = new Glyph();
                    g.text = String.valueOf(ch);
                    g.x = x;
                    g.y = y;
                    g.advance = step > 0 ? step : defaultStep;
                    g.height = height;
                    g.order = order;
                    out.add(g);
                }
                x += step;
                y += pick(dy, i, 0);
            }
        }
        return true;
    }

    /** 行聚类 + 行内排序 + 拼接。 */
    private static List<String> buildLines(List<Glyph> glyphs) {
        double tolerance = Math.max(
                HEIGHT_TOLERANCE_RATIO * medianHeight(glyphs),
                MIN_HEIGHT_TOLERANCE_MM);

        // 先按 y 再按 x 排序，随后做顺序聚类：同一行的字符 y 相差很小。
        glyphs.sort(Comparator.comparingDouble((Glyph g) -> g.y)
                .thenComparingDouble(g -> g.x)
                .thenComparingInt(g -> g.order));

        List<List<Glyph>> rows = new ArrayList<>();
        List<Glyph> current = new ArrayList<>();
        double sum = 0;
        for (Glyph g : glyphs) {
            if (current.isEmpty()) {
                current.add(g);
                sum = g.y;
                continue;
            }
            if (Math.abs(g.y - sum / current.size()) <= tolerance) {
                current.add(g);
                sum += g.y;
            } else {
                rows.add(current);
                current = new ArrayList<>();
                current.add(g);
                sum = g.y;
            }
        }
        if (!current.isEmpty()) {
            rows.add(current);
        }

        List<String> lines = new ArrayList<>(rows.size());
        for (List<Glyph> row : rows) {
            row.sort(Comparator.comparingDouble((Glyph g) -> g.x).thenComparingInt(g -> g.order));
            StringBuilder sb = new StringBuilder();
            Glyph prev = null;
            for (Glyph g : row) {
                if (prev != null) {
                    double gap = g.x - (prev.x + prev.advance);
                    if (gap > SPACE_GAP_RATIO * Math.max(prev.height, g.height)) {
                        sb.append(' ');
                    }
                }
                sb.append(g.text);
                prev = g;
            }
            String line = stripTrailing(sb.toString());
            if (!line.trim().isEmpty()) {
                lines.add(line);
            }
        }
        return lines;
    }

    private static String joinContent(List<TextCode> codes) {
        StringBuilder sb = new StringBuilder();
        for (TextCode code : codes) {
            String text = code.getContent();
            if (text != null) {
                sb.append(text);
            }
        }
        return sb.toString().replace("\r", "").replace("\n", "");
    }

    private static double medianHeight(List<Glyph> glyphs) {
        List<Double> heights = new ArrayList<>(glyphs.size());
        for (Glyph g : glyphs) {
            if (g.height > 0) {
                heights.add(g.height);
            }
        }
        if (heights.isEmpty()) {
            return 1.0;
        }
        heights.sort(Comparator.naturalOrder());
        int n = heights.size();
        return n % 2 == 1 ? heights.get(n / 2) : (heights.get(n / 2 - 1) + heights.get(n / 2)) / 2.0;
    }

    private static String stripTrailing(String s) {
        int end = s.length();
        while (end > 0 && Character.isWhitespace(s.charAt(end - 1))) {
            end--;
        }
        return s.substring(0, end);
    }

    /** 取数组第 i 项；数组只有 1 个值时视为对所有字符统一生效；越界则沿用末项。 */
    private static double pick(double[] values, int i, double fallback) {
        if (values.length == 0) {
            return fallback;
        }
        if (values.length == 1) {
            return values[0];
        }
        return i < values.length ? values[i] : values[values.length - 1];
    }

    private static double[] toDoubles(ST_Array array) {
        if (array == null) {
            return new double[0];
        }
        try {
            Double[] values = array.toDouble();
            if (values == null) {
                return new double[0];
            }
            int n = 0;
            for (Double v : values) {
                if (v != null) {
                    n++;
                }
            }
            double[] out = new double[n];
            int i = 0;
            for (Double v : values) {
                if (v != null) {
                    out[i++] = v;
                }
            }
            return out;
        } catch (RuntimeException e) {
            // 数组里混入了非数字（部分生成器会写出 "null"），退化为按均分估算
            return new double[0];
        }
    }

    /** CTM 为空或单位矩阵时返回 true。 */
    private static boolean isIdentityCtm(ST_Array ctm) {
        if (ctm == null) {
            return true;
        }
        double[] m = toDoubles(ctm);
        if (m.length == 0) {
            return true;
        }
        if (m.length != 6) {
            return false;
        }
        double[] unit = {1, 0, 0, 1, 0, 0};
        for (int i = 0; i < 6; i++) {
            if (Math.abs(m[i] - unit[i]) > 1e-6) {
                return false;
            }
        }
        return true;
    }

    private static double num(Double v, double fallback) {
        return v == null ? fallback : v;
    }
}
