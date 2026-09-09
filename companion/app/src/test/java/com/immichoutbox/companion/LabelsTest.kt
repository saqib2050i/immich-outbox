package com.immichoutbox.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.assertFalse
import org.junit.Test

/**
 * The strings here were read off a real Pixel running Google Photos, screen
 * by screen. The app's first release looked for "free up space" and never
 * found the menu entry, because it is called "Free up space on this device"
 * -- a difference no amount of care in the service would have caught, and
 * that this file would have.
 */
class LabelsTest {

    // ---- the path through the app ---------------------------------------

    @Test fun `the account menu entry is recognised`() {
        assertTrue(Labels.matches("Free up space on this device", Labels.ENTRY))
    }

    @Test fun `the blue button is recognised and carries the figure`() {
        val m = Labels.ACTION.find("Free up 29.80 MB")
        assertEquals("29.80", m!!.groupValues[1])
        assertEquals("MB", m.groupValues[2])
    }

    @Test fun `the finished screen reports what was freed`() {
        assertEquals(31247564L, Labels.freedBytes("You freed up 29.80 MB"))
    }

    @Test fun `nothing to free up is recognised`() {
        assertTrue(Labels.isNothingToDo("Nothing to free up"))
        assertTrue(Labels.isNothingToDo(
            "Nothing to free up Come back after you back up more photos & videos"))
    }

    // ---- telling the screens apart --------------------------------------

    @Test fun `the menu entry is not mistaken for the button that acts`() {
        assertFalse(Labels.ACTION.containsMatchIn("Free up space on this device"))
    }

    @Test fun `the heading on the free-up screen is not mistaken for the button`() {
        // "Your device storage is 30% full, free up space" sits directly
        // above the real button. Tapping it does nothing useful.
        assertFalse(Labels.ACTION.containsMatchIn(
            "Your device storage is 30% full, free up space"))
    }

    @Test fun `the button is not mistaken for the finished screen`() {
        assertNull(Labels.freedBytes("Free up 29.80 MB"))
    }

    @Test fun `an unrelated screen matches nothing`() {
        val junk = "Backup complete Selfies Me 7 years ago"
        assertFalse(Labels.matches(junk, Labels.ENTRY))
        assertFalse(Labels.ACTION.containsMatchIn(junk))
        assertFalse(Labels.isNothingToDo(junk))
    }

    // ---- sizes -----------------------------------------------------------

    @Test fun `sizes parse in every unit Photos uses`() {
        assertEquals(1024L, Labels.bytes("1", "KB"))
        assertEquals(1048576L, Labels.bytes("1", "MB"))
        assertEquals(1073741824L, Labels.bytes("1", "GB"))
        assertEquals(1610612736L, Labels.bytes("1.5", "GB"))
    }

    @Test fun `a grouped thousands figure parses`() {
        assertEquals(Labels.bytes("1234", "MB"), Labels.bytes("1,234", "MB"))
    }

    @Test fun `a size on its own line is still found`() {
        assertEquals(2147483648L, Labels.freedBytes("You freed up 2 GB"))
    }

    // ---- the figure is never fixed --------------------------------------

    @Test fun `the button is matched at any size Photos might show`() {
        // Whatever is free at that instant: a few megabytes after a quiet
        // day, several gigabytes after a backlog goes through.
        listOf(
            "Free up 512 KB",
            "Free up 29.80 MB",
            "Free up 950 MB",
            "Free up 1.2 GB",
            "Free up 14.7 GB",
            "Free up 1,024 MB",
            "Free up 3GB",
        ).forEach {
            assertTrue(it, Labels.ACTION.containsMatchIn(it))
        }
    }

    @Test fun `the freed figure is read back at any size`() {
        assertEquals(524288L, Labels.freedBytes("You freed up 512 KB"))
        assertEquals(996147200L, Labels.freedBytes("You freed up 950 MB"))
        assertEquals(1288490188L, Labels.freedBytes("You freed up 1.2 GB"))
        assertEquals(15784004812L, Labels.freedBytes("You freed up 14.7 GB"))
    }

    @Test fun `a decimal comma is not read as a thousands separator`() {
        // "29,80 MB" is how much of Europe writes it. Stripping the comma
        // turned 29.8 MB into 2,980 MB.
        assertEquals(Labels.freedBytes("You freed up 29.80 MB"),
                     Labels.freedBytes("You freed up 29,80 MB"))
    }

    @Test fun `a thousands separator still is one`() {
        assertEquals(1024L * 1024 * 1024, Labels.freedBytes("You freed up 1,024 MB"))
    }

    // ---- the account picture --------------------------------------------

    @Test fun `common descriptions of the account picture are recognised`() {
        listOf("Account and settings", "Signed in as Saqib", "Profile picture")
            .forEach { assertTrue(it, Labels.matches(it, Labels.MENU)) }
    }

    // ---- formatting ------------------------------------------------------

    @Test fun `sizes are formatted for a person`() {
        assertEquals("29.8 MB", Labels.format(31247564L))
        assertEquals("1.0 GB", Labels.format(1073741824L))
    }
}
