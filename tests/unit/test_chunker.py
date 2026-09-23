"""Unit tests for the text chunker."""

from services.chunker import (
    MAX_CHARS,
    MAX_CHUNK_CHARS,
    MIN_CHARS,
    OVERLAP_CHARS,
    _estimate_tokens,
    chunk_pages,
    chunk_text,
)


class TestChunkText:

    def test_empty_content_returns_nothing(self):
        assert chunk_text("") == []
        assert chunk_text("   ") == []
        assert chunk_text(None) == []

    def test_short_text_returns_one_chunk(self):
        text = "This is a paragraph with enough words to comfortably exceed the minimum token threshold. " * 3
        text = text.strip()
        chunks = chunk_text(text)
        assert len(chunks) == 1
        assert chunks[0].index == 0
        assert chunks[0].page is None

    def test_tiny_single_chunk_is_kept(self):
        # The doc's only content must survive — dropping it would erase the
        # doc from FTS entirely. Min-merge only applies when a neighbor exists.
        chunks = chunk_text("hi")
        assert len(chunks) == 1
        assert chunks[0].content == "hi"

    def test_long_text_produces_multiple_chunks(self):
        para = "Word " * 300
        text = f"{para}\n\n{para}\n\n{para}"
        chunks = chunk_text(text)
        assert len(chunks) > 1

    def test_chunks_have_sequential_indices(self):
        text = ("Paragraph of text. " * 50 + "\n\n") * 10
        chunks = chunk_text(text)
        for i, chunk in enumerate(chunks):
            assert chunk.index == i

    def test_header_breadcrumb_tracking(self):
        text = "# Main Title\n\nIntro paragraph.\n\n## Section A\n\n" + ("Content. " * 50)
        chunks = chunk_text(text)
        assert len(chunks) >= 1
        assert "Main Title" in chunks[-1].header_breadcrumb
        assert "Section A" in chunks[-1].header_breadcrumb

    def test_page_parameter_propagates(self):
        text = "Enough content to make a chunk. " * 10
        chunks = chunk_text(text, page=5)
        assert all(c.page == 5 for c in chunks)

    def test_start_char_offset(self):
        text = "Enough content to make a chunk. " * 10
        chunks = chunk_text(text, start_char_offset=100)
        assert chunks[0].start_char >= 100

    def test_token_count_is_positive(self):
        text = "A reasonable paragraph with sufficient words. " * 10
        chunks = chunk_text(text)
        assert all(c.token_count > 0 for c in chunks)


class TestCharBudget:
    """The 500-char cap exists for bge-large-zh-v1.5's 512-token input limit
    (CJK ≈ 1 token/char). Non-atomic chunks must never exceed it."""

    def test_cjk_paragraph_split_within_budget(self):
        text = "这是一段中文正文，用来验证切分预算。" * 60  # ~1000 chars, no blank lines
        chunks = chunk_text(text)
        assert len(chunks) >= 2
        assert all(len(c.content) <= MAX_CHARS for c in chunks)

    def test_cap_is_budget_not_guillotine(self):
        # Sentences end with 。 — splits should land on sentence boundaries,
        # not mid-sentence at exactly 500.
        text = "".join(f"这是第{i}句完整的句子。" for i in range(80))
        chunks = chunk_text(text)
        assert all(c.content.endswith("。") for c in chunks[:-1])

    def test_hard_slice_only_as_last_resort(self):
        text = "字" * 1200  # no boundary of any kind
        chunks = chunk_text(text)
        assert len(chunks) == 3  # 1200 / 500, ceiling
        assert all(len(c.content) <= MAX_CHARS for c in chunks)
        # Contiguous: nothing lost, nothing duplicated
        assert "".join(c.content for c in chunks) == text


class TestAtomicBlocks:

    def test_code_fence_never_torn(self):
        code = "\n".join(f"line {i}: x = {i}" for i in range(50))  # ~800 chars
        text = f"介绍段落，足够长的内容用来占位说明这段文字的意义。\n\n```python\n{code}\n```\n\n" + "后续正文内容。" * 30
        chunks = chunk_text(text)
        code_chunks = [c for c in chunks if "line 0: x = 0" in c.content]
        assert len(code_chunks) == 1
        assert code_chunks[0].oversized
        assert "line 49: x = 49" in code_chunks[0].content  # intact

    def test_fence_with_blank_lines_stays_whole(self):
        code = "a = 1\n\n\nb = 2"
        text = f"```\n{code}\n```"
        chunks = chunk_text(text)
        assert len(chunks) == 1
        assert "b = 2" in chunks[0].content

    def test_table_never_torn(self):
        table = "\n".join(f"| 列{i} | 值{i} |" for i in range(60))
        chunks = chunk_text(table)
        assert len(chunks) == 1
        assert chunks[0].oversized


class TestOverlapAndMerge:

    def test_consecutive_chunks_overlap(self):
        para = "机器学习是人工智能的一个分支，研究如何从数据中学习规律与模式。"
        text = "\n\n".join(para + str(i) for i in range(20))  # many ~35-char paragraphs
        chunks = chunk_text(text)
        assert len(chunks) >= 2
        for prev, cur in zip(chunks, chunks[1:]):
            shared = cur.content[:OVERLAP_CHARS]
            # The next chunk opens with a suffix of the previous chunk
            assert shared and shared[:10] in prev.content

    def test_overlap_stays_within_budget(self):
        para = "深度学习使用多层神经网络拟合复杂的非线性函数映射关系。"
        text = "\n\n".join(para + str(i) for i in range(20))
        chunks = chunk_text(text)
        for prev, cur in zip(chunks, chunks[1:]):
            # find longest prefix of cur that is a suffix-substring of prev
            best = 0
            for k in range(1, min(len(cur.content), OVERLAP_CHARS * 2) + 1):
                if cur.content[:k] in prev.content:
                    best = k
            assert best <= OVERLAP_CHARS + 40  # one paragraph of slack

    def test_tiny_trailing_chunk_merges(self):
        body = "正文段落，内容足够多以撑满一个正常的分块。" * 30
        text = f"{body}\n\n尾巴。"
        chunks = chunk_text(text)
        assert all(len(c.content) >= MIN_CHARS or len(chunks) == 1 for c in chunks)
        assert any("尾巴。" in c.content for c in chunks)


class TestEstimateTokens:

    def test_rough_estimate(self):
        assert _estimate_tokens("a" * 400) == 100
        assert _estimate_tokens("") == 1

    def test_cjk_counts_one_per_char(self):
        assert _estimate_tokens("中" * 400) == 400


class TestChunkPages:

    def test_multiple_pages(self):
        pages = [
            (1, "First page content. " * 20),
            (2, "Second page content. " * 20),
        ]
        chunks = chunk_pages(pages)
        assert len(chunks) >= 2
        page_nums = {c.page for c in chunks}
        assert 1 in page_nums
        assert 2 in page_nums

    def test_indices_are_global(self):
        pages = [
            (1, "Content A. " * 20),
            (2, "Content B. " * 20),
        ]
        chunks = chunk_pages(pages)
        indices = [c.index for c in chunks]
        assert indices == list(range(len(chunks)))


class TestSplitPiecesHavePerPieceStartChar:
    """Regression: pieces split from one oversized source span must carry
    distinct, increasing start_chars or text-anchor → chunk mapping breaks."""

    def test_split_pieces_have_increasing_offsets(self):
        sentence = "All this happened, more or less. " * 6
        paragraph = (sentence * 200).strip()
        assert len(paragraph) > MAX_CHUNK_CHARS

        chunks = chunk_text(paragraph, page=7, start_char_offset=1000)
        assert all(len(c.content) <= MAX_CHUNK_CHARS for c in chunks)

        starts = [c.start_char for c in chunks]
        assert starts[0] == 1000
        assert len(set(starts)) == len(starts), (
            "Split pieces share start_char; downstream text-anchor mapping will "
            "misassign highlights."
        )
        for a, b in zip(starts, starts[1:]):
            assert b > a

    def test_under_limit_chunks_unchanged(self):
        text = "This is a properly sized paragraph with enough words to actually be chunked. " * 4
        chunks = chunk_text(text.strip(), page=1, start_char_offset=500)
        assert len(chunks) >= 1
        for c in chunks:
            assert c.start_char is not None
